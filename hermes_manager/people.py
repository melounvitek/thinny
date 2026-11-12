"""Adding, changing and removing people. Everything inside a person's home runs as that person."""

from __future__ import annotations

import base64
import hashlib
import json
import pwd
import secrets
import time
from pathlib import Path

from . import host, render
from .registry import Person, Registry, RegistryError
from .settings import ACCESS_METHODS, Settings
from .system import (CommandError, as_user, as_user_interactive, gateway_active, http_status, log, run,
                     tailscale_name, uid_of, write_user_file)

AUTH_PREFIX = "HERMES_DASHBOARD_BASIC_AUTH_"
RESTART_TIMEOUT = 45 * 60  # a restart waits for a running reply (up to 30 min by default)


def hermes(name: str, args: list[str], **kw):
    return as_user(name, ["hermes", *args], **kw)


def password_hash(password: str) -> str:
    """The hash format of Hermes's basic sign-in (plugins/dashboard_auth/basic)."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=0)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def home(name: str) -> Path:
    return Path(pwd.getpwnam(name).pw_dir) / ".hermes"


def url_for(s: Settings, p: Person) -> str:
    ts = tailscale_name() if p.access == "tailscale" else ""
    return render.person_url(s, p.name, p.access, p.port, url=p.url, tailscale_name=ts)


# ---------- pieces ----------

def ensure_account(name: str) -> int:
    if uid_of(name) is None:
        run(["useradd", "--create-home", "--shell", "/bin/bash", "--key", "HOME_MODE=0700", name])
    pw = pwd.getpwnam(name)
    Path(pw.pw_dir).chmod(0o700)
    run(["loginctl", "enable-linger", name])
    bus = Path(f"/run/user/{pw.pw_uid}/bus")
    for _ in range(60):
        if bus.exists():
            return pw.pw_uid
        time.sleep(1)
    raise CommandError(f"the user manager of {name} did not start")


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
    changed = False
    for key, value in wanted.items():
        if current.get(key) != value:
            hermes(p.name, ["config", "set", f"webapp.{key}", value])
            changed = True
    return changed


def _env_lines(name: str) -> list[str]:
    """The person's .env, read as the person: as root, a planted symlink could pull another
    file's content into it."""
    return as_user(name, ["cat", str(home(name) / ".env")], check=False, timeout=30).stdout.splitlines()


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
sys.path.insert(0, "/opt/hermes/hermes-agent")
from hermes_cli.ui_preferences import store_path, write_preferences
values = json.load(sys.stdin)
write_preferences(values, path=store_path(Path(sys.argv[1])))
'''


def seed_ui(s: Settings, name: str) -> None:
    """Webapp look from the settings, through Hermes's own store (same lock and revision bump)."""
    pythons = sorted(home(name).glob("installs/*/environments/*/venv/bin/python3"), key=lambda p: p.stat().st_mtime)
    if not pythons:
        raise CommandError(f"no Hermes environment for {name}; run: hermes-manager add-person {name}")
    as_user(name, [str(pythons[-1]), "-I", "-c", SEED_UI, str(home(name))],
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
    unit = home(p.name).parent / ".config/systemd/user/hermes-gateway.service"
    if unit.exists():
        hermes(p.name, ["gateway", "restart", "--yes"], timeout=RESTART_TIMEOUT)
    else:
        hermes(p.name, ["gateway", "install"], timeout=600)
    check_gateway(p)


def restart_all(people: list[Person]) -> None:
    """One gateway at a time (each waits for a running reply); stop at the first failure."""
    for p in people:
        log(f"restarting {p.name} (port {p.port})")
        start_or_restart_gateway(p)
        log(f"  ok {p.name}")


# ---------- commands ----------

def add(s: Settings, reg: Registry, name: str, access: str | None, url: str = "") -> None:
    access = access or s.access_default
    if access not in ACCESS_METHODS:
        raise CommandError(f"access must be one of {', '.join(ACCESS_METHODS)}")
    if access == "url" and not url.startswith(("https://", "http://")):
        raise CommandError("url access needs the address, e.g. --access url=https://alice.example.org/")
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
        if uid_of(name) is not None:
            raise CommandError(f"a Linux account {name!r} already exists; choose another name")
        p = reg.add(name, access, s.port_range)
    p.access, p.url = access, url if access == "url" else ""
    reg.save()

    log(f"{name}: account")
    p.uid = ensure_account(name)
    reg.save()
    host.firewall(s, reg)  # the user slice exists now, so the loopback rule can load
    log(f"{name}: Hermes environment (hermes pm repair)")
    hermes(name, ["pm", "repair"], timeout=1800)
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
        raise CommandError("url access needs the address, e.g. --access url=https://alice.example.org/")
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
    """Disable a person: everything stops and the account is locked; their data stays."""
    p = reg.get(name)
    if p.state == "disabled":
        print(f"{name} is already removed")
        return
    if uid_of(name) is not None:
        as_user(name, ["systemctl", "--user", "disable", "--now", "hermes-gateway"], check=False, timeout=300)
        as_user(name, ["podman", "rm", "--all", "--force", "--time", "10"], check=False, timeout=300)
        run(["loginctl", "disable-linger", name], check=False)
        run(["loginctl", "terminate-user", name], check=False)
        run(["usermod", "--lock", "--expiredate", "1", name])
    p.state = "disabled"
    reg.save()
    host.reconcile_access(s, reg)
    host.firewall(s, reg)
    print(f"{name} removed: account locked, Webapp and agent stopped. Their data stays in "
          f"{pwd.getpwnam(name).pw_dir if uid_of(name) is not None else '(no home)'}.")


def list_people(reg: Registry) -> None:
    rows = [("NAME", "STATE", "ACCESS", "PORT", "GATEWAY", "URL")]
    for p in sorted(reg.people.values(), key=lambda p: p.port):
        gateway = "-" if p.state == "disabled" or uid_of(p.name) is None else \
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
