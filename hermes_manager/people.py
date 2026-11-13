"""Adding, changing and removing people. Each person has their own Incus container; everything in
their home runs as them, inside it."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import PurePosixPath

from . import host, render
from .registry import Person, Registry, RegistryError
from .settings import ACCESS_METHODS, Settings
from .system import (PERSON_UID, STATE_DIR, CommandError, as_user, as_user_interactive, gateway_active, http_status,
                     in_container, log, read_user_file, run, tailscale_name, uid_of, write_file, write_user_file)

AUTH_PREFIX = "HERMES_DASHBOARD_BASIC_AUTH_"
RESTART_TIMEOUT = 45 * 60  # a restart waits for a running reply (up to 30 min by default)
RESTART_PENDING = STATE_DIR / "restart-pending"
IMAGE = "images:debian/13"
PROFILE = "hm-person"


def hermes(name: str, args: list[str], **kw):
    return as_user(name, ["hermes", *args], **kw)


def password_hash(password: str) -> str:
    """The hash format of Hermes's basic sign-in (plugins/dashboard_auth/basic)."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=0)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def home(name: str) -> PurePosixPath:
    """The person's Hermes home inside their container (its path names their Webapp cookies)."""
    return PurePosixPath(f"/home/{name}/.hermes")


def url_for(s: Settings, p: Person) -> str:
    ts = tailscale_name() if p.access == "tailscale" else ""
    return render.person_url(s, p.name, p.access, p.port, url=p.url, tailscale_name=ts)


# ---------- pieces ----------

def container_state(name: str) -> str | None:
    """RUNNING, STOPPED, ... or None when there is no such container."""
    out = run(["incus", "list", "--format", "csv", "--columns", "s", f"^{name}$"]).stdout.strip()
    return out or None


def devices(name: str) -> set[str]:
    return set(run(["incus", "config", "device", "list", name]).stdout.split())


def add_device(name: str, device: str, *args: str) -> None:
    if device not in devices(name):
        run(["incus", "config", "device", "add", name, device, *args])


def ensure_container(p: Person) -> None:
    """The person's container from the plain Debian image, running, with its network, the
    read-only policy and the Webapp published on the host's loopback (each step repeatable)."""
    if container_state(p.name) is None:
        run(["incus", "init", IMAGE, p.name, "--profile", PROFILE], timeout=1800)
    add_device(p.name, "eth0", "nic", "nictype=routed", f"ipv4.address={p.address}", f"host_name=hm-{p.port}")
    add_device(p.name, "policy", "disk", f"source={host.MANAGED}", f"path={host.MANAGED}", "readonly=true", "shift=true")
    add_device(p.name, "webapp", "proxy", f"listen=tcp:127.0.0.1:{p.port}", f"connect=tcp:127.0.0.1:{p.port}")
    if container_state(p.name) != "RUNNING":
        run(["incus", "start", p.name])
    in_container(p.name, ["systemctl", "is-system-running", "--wait"], check=False, timeout=300)
    in_container(p.name, ["sh", "-c", "cat > /etc/systemd/network/eth0.network && systemctl restart systemd-networkd"],
                 input=render.network_file(p.address))


def ensure_packages(name: str) -> None:
    try:
        in_container(name, ["sh", "-c", 'export DEBIAN_FRONTEND=noninteractive && apt-get update -q && '
                            'apt-get install -y -q --no-install-recommends "$@" && apt-get clean',
                            "sh", *render.PACKAGES], timeout=3600)
    except CommandError as e:
        raise CommandError(f"{e}\nNo internet in {name}'s container? See README, Requirements "
                           "(the forward chain).") from None


def ensure_account(name: str) -> None:
    """The person's account in their container: passwordless sudo, user manager kept running."""
    if in_container(name, ["id", "-u", name], check=False).returncode != 0:
        in_container(name, ["useradd", "--create-home", "--shell", "/bin/bash", "--uid", str(PERSON_UID),
                            "--groups", "sudo", name])
    in_container(name, ["sh", "-c", "cat > /etc/sudoers.d/hermes-manager && chmod 440 /etc/sudoers.d/hermes-manager"],
                 input=f"{name} ALL=(ALL) NOPASSWD:ALL\n")
    in_container(name, ["loginctl", "enable-linger", name])
    bus = f"/run/user/{PERSON_UID}/bus"
    for _ in range(60):
        if in_container(name, ["test", "-S", bus], check=False).returncode == 0:
            return
        time.sleep(1)
    raise CommandError(f"the user manager of {name} did not start")


def install_hermes(s: Settings, name: str, commit: str) -> None:
    """Hermes at exactly `commit` with the official installer (a first install or an update),
    the Webapp and the language pack that release's catalog pins."""
    write_user_file(name, "/tmp/hermes-install.sh", host.installer())
    as_user(name, ["bash", "/tmp/hermes-install.sh", "--channel", "stable", "--commit", commit, "--non-interactive"],
            timeout=3600)
    hermes(name, ["webapp", "--build-only"], timeout=3600)  # the installer does not build it
    if s.language_pack:
        hermes(name, ["plugins", "install", s.language_pack, "--force", "--yes-deps", "--no-enable"], timeout=1800)
    hermes(name, ["pm", "gc"], check=False, timeout=600)


def hermes_installed(name: str) -> bool:
    return as_user(name, ["test", "-d", str(home(name) / "hermes-agent/.git")], check=False).returncode == 0


def ensure_model_guard(name: str) -> None:
    """The server's model guard as a bundled plugin, read-only, hidden from the checkout's git."""
    add_device(name, "model-guard", "disk", f"source={host.FILES / 'model-guard'}",
               f"path={home(name) / 'hermes-agent/plugins/model-guard'}", "readonly=true", "shift=true")
    as_user(name, ["sh", "-c", 'grep -qx /plugins/model-guard/ "$1" || echo /plugins/model-guard/ >> "$1"', "sh",
                   str(home(name) / "hermes-agent/.git/info/exclude")])


def webapp_settings(name: str) -> dict[str, str]:
    """The person's `webapp:` settings as text (`hermes config get webapp`)."""
    out = hermes(name, ["config", "get", "webapp"], timeout=600).stdout
    current = {}
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            current[key.strip()] = value.strip().strip("'\"")
    current["enabled"] = current.get("enabled", "").lower()
    return current


def configure_webapp(s: Settings, p: Person) -> bool:
    """Webapp settings in the person's config; True when something changed."""
    wanted = {"enabled": "true", "host": "127.0.0.1", "port": str(p.port), "public_url": p.url}
    current = webapp_settings(p.name)
    changes = {key: value for key, value in wanted.items() if current.get(key) != value}
    if changes:
        mark_restart([p.name])
    for key, value in changes.items():
        hermes(p.name, ["config", "set", f"webapp.{key}", value])
    return bool(changes)


def _env_lines(name: str) -> list[str]:
    return read_user_file(name, str(home(name) / ".env")).splitlines()


def ensure_sign_in(name: str) -> str | None:
    """Webapp username and password in the person's .env; returns a new password, or None
    when they already have one."""
    lines = _env_lines(name)
    if any(line.startswith(AUTH_PREFIX + "USERNAME=") for line in lines):
        return None
    return _write_sign_in(name, lines)


def reset_password(name: str) -> str:
    return _write_sign_in(name, [line for line in _env_lines(name) if not line.startswith(AUTH_PREFIX)])


def _write_sign_in(name: str, lines: list[str]) -> str:
    password = secrets.token_urlsafe(12)
    lines = lines + [f"{AUTH_PREFIX}USERNAME={name}",
                     f"{AUTH_PREFIX}PASSWORD_HASH='{password_hash(password)}'",  # quoted: '$' in the hash
                     f"{AUTH_PREFIX}SECRET={secrets.token_urlsafe(32)}"]
    write_user_file(name, str(home(name) / ".env"), "\n".join(lines) + "\n")
    return password


SEED_UI = r'''
import json, sys
from pathlib import Path
home = Path(sys.argv[1])
sys.path.insert(0, str(home / "hermes-agent"))
from hermes_cli.ui_preferences import store_path, write_preferences
write_preferences(json.load(sys.stdin), path=store_path(home))
'''


def seed_ui(s: Settings, name: str) -> None:
    """Webapp look from the settings, through Hermes's own store (same lock and revision bump),
    with the newest Python environment of the person's Hermes."""
    as_user(name, ["sh", "-c", 'py=$(ls -t "$2"/installs/*/environments/*/venv/bin/python3 | head -n 1) && '
                   'exec "$py" -I -c "$1" "$2"', "sh", SEED_UI, str(home(name))],
            input=json.dumps(render.ui_preferences(s)))


def check_gateway(p: Person, timeout: float = 180) -> None:
    """Gateway active, Webapp answering, model-guard enabled; raise otherwise."""
    deadline = time.time() + timeout
    while True:
        active = gateway_active(p.name)
        code = http_status(f"http://127.0.0.1:{p.port}/")
        if active and code[0] in "23":
            break
        if time.time() > deadline:
            raise CommandError(f"{p.name}: gateway {'active' if active else 'not active'}, Webapp HTTP {code}")
        time.sleep(5)
    plugins = hermes(p.name, ["plugins", "list"], timeout=600).stdout
    row = next((line for line in plugins.splitlines() if "model-guard" in line), "")
    if "enabled" not in row or "not enabled" in row or "disabled" in row:
        raise CommandError(f"{p.name}: model-guard is not enabled")


def start_or_restart_gateway(p: Person) -> None:
    _gateway_up(p)
    _set_pending([n for n in _pending() if n != p.name])


def _gateway_up(p: Person) -> None:
    unit = home(p.name).parent / ".config/systemd/user/hermes-gateway.service"
    if as_user(p.name, ["test", "-e", str(unit)], check=False).returncode == 0:
        hermes(p.name, ["gateway", "restart", "--yes"], timeout=RESTART_TIMEOUT)
    else:
        hermes(p.name, ["gateway", "install"], timeout=600)
    check_gateway(p)


def _pending() -> list[str]:
    return RESTART_PENDING.read_text().split() if RESTART_PENDING.exists() else []


def _set_pending(names: list[str]) -> None:
    if names:
        write_file(RESTART_PENDING, "".join(f"{n}\n" for n in names), 0o600)
    else:
        RESTART_PENDING.unlink(missing_ok=True)


def mark_restart(names: list[str]) -> None:
    """Remember gateways that must restart, before changing what they read: a failure on the
    way leaves them for the next restart_all (apply, update or restart)."""
    _set_pending(list(dict.fromkeys([*_pending(), *names])))


def restart_all(people: list[Person], reg: Registry) -> None:
    """Restart these gateways and every remembered one, one at a time (each waits for a
    running reply); stop at the first failure, leaving the rest remembered."""
    mark_restart([p.name for p in people])
    for name in _pending():
        p = reg.people.get(name)
        if p is None or p.state != "active":
            _set_pending([n for n in _pending() if n != name])
            continue
        log(f"restarting {p.name} (port {p.port})")
        start_or_restart_gateway(p)
        log(f"  ok {p.name}")


# ---------- commands ----------

def add(s: Settings, reg: Registry, name: str, access: str | None, url: str = "") -> None:
    access = access or s.access_default
    if access not in ACCESS_METHODS:
        raise CommandError(f"access must be one of {', '.join(ACCESS_METHODS)}")
    if access == "url" and not url.startswith(("https://", "http://")):
        raise CommandError("url access needs the address, e.g. url=https://alice.example.org/")
    if access == "lan" and not s.lan_address:
        raise CommandError("set access.lan_address in site.toml first")
    if name in reg.people:
        p = reg.get(name)
        if p.state == "active":
            raise CommandError(f"{name} already exists (use: hermes-manager access {name} ...)")
        if p.state == "disabled":
            raise CommandError(f"{name} was removed; names are not reused")
        log(f"resuming the unfinished setup of {name}")
    else:
        if uid_of(name) is not None:  # Debian's system accounts, which the container has too
            raise CommandError(f"{name!r} is a system account name; choose another name")
        if container_state(name) is not None:
            raise CommandError(f"an Incus container {name!r} already exists; choose another name")
        p = reg.add(name, access, s.port_range)
    p.access, p.url = access, url if access == "url" else ""
    reg.save()

    log(f"{name}: container {p.address} (packages take a few minutes)")
    ensure_container(p)
    ensure_packages(name)
    ensure_account(name)
    if not hermes_installed(name):
        tag, commit = host.release()
        log(f"{name}: Hermes {tag} (takes a few minutes)")
        install_hermes(s, name, commit)
    ensure_model_guard(name)
    p.url = url_for(s, p)
    reg.save()
    configure_webapp(s, p)
    hermes(name, ["config", "set", "display.language", s.language])
    password = ensure_sign_in(name)
    seed_ui(s, name)
    host.reconcile_access(s, reg)
    log(f"{name}: gateway")
    start_or_restart_gateway(p)
    p.state = "active"
    reg.save()

    print(f"\n{name} is ready.\n  Webapp:   {p.url}\n  Username: {name}")
    print(f"  Password: {password}   (shown once; reset with: hermes-manager password {name})" if password
          else "  Password: unchanged (reset with: hermes-manager password " + name + ")")
    _print_access_hint(p)
    print(f"Next: sign {name} in to {s.model_provider} before handing over: hermes-manager login {name}")


def _print_access_hint(p: Person) -> None:
    if p.access == "url":
        print(f"  Point your proxy or tunnel at http://127.0.0.1:{p.port} (see README, Access).")
    elif p.access == "local":
        print(f"  Open with: ssh -L {p.port}:127.0.0.1:{p.port} root@<server>, then browse to {p.url}")
    elif p.access == "lan":
        print(f"  Office PCs should trust Caddy's certificate: {host.caddy_root_ca()} (see README).")


def change_access(s: Settings, reg: Registry, name: str, access: str, url: str = "") -> None:
    p = _active(reg, name)
    if access not in ACCESS_METHODS:
        raise CommandError(f"access must be one of {', '.join(ACCESS_METHODS)}")
    if access == "url" and not url.startswith(("https://", "http://")):
        raise CommandError("url access needs the address, e.g. url=https://alice.example.org/")
    p.access, p.url = access, url if access == "url" else ""
    p.url = url_for(s, p)
    reg.save()
    configure_webapp(s, p)
    host.reconcile_access(s, reg)
    start_or_restart_gateway(p)
    print(f"{name}: {p.url}")
    _print_access_hint(p)


def login(s: Settings, reg: Registry, name: str) -> None:
    _active(reg, name)
    print(f"Signing {name} in to {s.model_provider}. Give the code below to {name}; it expires in about 15 minutes.")
    if as_user_interactive(name, ["hermes", "auth", "add", s.model_provider, "--type", "oauth", "--no-browser"]):
        raise CommandError("the login did not finish")


def password(reg: Registry, name: str) -> None:
    p = _active(reg, name)
    new = reset_password(name)
    start_or_restart_gateway(p)
    print(f"{name}: new Webapp password: {new}   (shown once; open browsers must sign in again)")


def remove(s: Settings, reg: Registry, name: str) -> None:
    """Disable a person: their container stops for good; their data stays in it."""
    p = reg.get(name)
    if p.state == "disabled":
        print(f"{name} is already removed")
        return
    if container_state(name) is not None:
        run(["incus", "config", "set", name, "boot.autostart=false"])  # first: a reboot must not bring it back
        if container_state(name) == "RUNNING" and \
                run(["incus", "stop", name, "--timeout", "60"], check=False, timeout=120).returncode != 0:
            run(["incus", "stop", name, "--force"])  # its root may refuse to shut down
        if container_state(name) == "RUNNING":
            raise CommandError(f"{name}'s container is still running")
    p.state = "disabled"
    reg.save()
    host.reconcile_access(s, reg)
    print(f"{name} removed: container stopped, Webapp and agent with it. Their data stays in the container {name}.")


def list_people(reg: Registry) -> None:
    rows = [("NAME", "STATE", "ACCESS", "PORT", "GATEWAY", "URL")]
    for p in sorted(reg.people.values(), key=lambda p: p.port):
        gateway = "-" if p.state == "disabled" or container_state(p.name) is None else \
            ("active" if gateway_active(p.name) else "INACTIVE")
        rows.append((p.name, p.state, p.access, str(p.port), gateway, p.url))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _active(reg: Registry, name: str) -> Person:
    p = reg.get(name)
    if p.state != "active":
        raise RegistryError(f"{name} is {p.state}")
    return p
