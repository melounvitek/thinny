"""Adding, changing and removing instances. Each instance has its own Incus container; everything in
its home runs as the instance's account, inside it."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import math
import re
import secrets
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import host, render, trace
from .registry import Instance, Registry, RegistryError
from .settings import ACCESS_METHODS, Settings
from .system import (INSTANCE_UID, STATE_DIR, CommandError, as_user, as_user_interactive, gateway_active, http_status,
                     in_container, log, read_user_file, run, step, tailscale_name, uid_of, write_file,
                     write_user_file)

AUTH_PREFIX = "HERMES_DASHBOARD_BASIC_AUTH_"
RESTART_TIMEOUT = 45 * 60  # a restart waits for a running reply (up to 30 min by default)
RESTART_PENDING = STATE_DIR / "restart-pending"
IMAGE = "images:debian/13"
URL_RE = re.compile(r"https?://[a-z0-9.-]+(:\d+)?(/[a-z0-9-]+)?/?")
CLAUDE_INSTALLER = "https://claude.ai/install.sh"
# The Claude subscription plugin's provider; it signs in through Claude Code.
CLAUDE_PROVIDER = "claude-subscription-directsdk-experimental"
# Host folders in a container. Incus does not lock a disk added to a running container read-only, and with
# shift its root could remount it and write into the folder as the host's root. Without shift, it is just
# another account there. "shift=false", not left out, so older disks lose their shift too.
READ_ONLY = ("readonly=true", "shift=false")


def hermes(name: str, args: list[str], **kw):
    return as_user(name, ["hermes", *args], **kw)


def password_hash(password: str) -> str:
    """The hash format of Hermes's basic sign-in (plugins/dashboard_auth/basic)."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=0)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def home(name: str) -> PurePosixPath:
    """The instance's Hermes home inside its container (its path names the Webapp cookies)."""
    return PurePosixPath(f"/home/{name}/.hermes")


def url_for(s: Settings, inst: Instance) -> str:
    ts = tailscale_name() if inst.access == "tailscale" else ""
    return render.instance_url(s, inst.name, inst.access, inst.port, url=inst.url, tailscale_name=ts)


# ---------- pieces ----------

def container_state(name: str) -> str | None:
    """RUNNING, STOPPED, ... or None when there is no such container."""
    out = run(["incus", "list", "--format", "csv", "--columns", "s", f"^{name}$"]).stdout.strip()
    return out or None


def devices(name: str) -> dict[str, dict[str, str]]:
    """The container's own devices (not its profile's): name -> properties."""
    return json.loads(run(["incus", "query", f"/1.0/instances/{name}"]).stdout)["devices"]


def settings_for(s: Settings, inst: Instance) -> Settings:
    """The settings with the instance's own model lock, when it has one."""
    if inst.lock is None:
        return s
    if not inst.lock:
        return dataclasses.replace(s, model_lock=False)
    return dataclasses.replace(s, model_lock=True, model_provider=inst.lock_provider, model=inst.lock[0],
                               allowed_models=tuple(inst.lock))


def update_policy(s: Settings, inst: Instance) -> None:
    """The instance's policy as the settings and its own lock say; a change restarts its gateway."""
    s = settings_for(s, inst)
    if not host.policy_current(s, inst.name):
        if inst.state == "active":
            log(f"{inst.name}: its policy changes: its gateway restarts")
            mark_restart([inst.name])  # before the change, so a failure keeps the intent
        if s.model_lock:
            # Before the lock pins them: Hermes drops pinned keys from an instance's config whenever it
            # saves it, and writes a `model:` section left empty as invalid YAML (v0.23.1). Pending
            # instances too: a resumed add would start its gateway under the lock.
            for key in starting_model(s):
                hermes(inst.name, ["config", "unset", key], check=False)  # 1 when not set
    ensure_policy(s, inst)


def ensure_policy(s: Settings, inst: Instance) -> None:
    """The instance's own policy, read-only as Hermes's managed scope in its container."""
    host.write_policy(s, inst.name)
    ensure_device(inst.name, "policy", "disk", f"source={host.POLICIES / inst.name}", f"path={host.MANAGED}",
                  *READ_ONLY)


def ensure_device(name: str, device: str, kind: str, *props: str) -> None:
    """The container's own `device` with these "key=value" properties, added or updated (an imported
    container comes with its old server's)."""
    current = devices(name).get(device)
    if current is None:
        run(["incus", "config", "device", "add", name, device, kind, *props])
    elif changed := [p for p in props if current.get(p.partition("=")[0]) != p.partition("=")[2]]:
        run(["incus", "config", "device", "set", name, device, *changed])


def set_limits(name: str, cpu: int | None, memory_mb: int | None, disk_gb: int | None) -> None:
    """The instance's own limits instead of the profile's (site.toml's [container]); None keeps
    the current one. A running container gets them at once."""
    if disk_gb:
        # btrfs's quota counts data shared with the image or a snapshot too, unlike Incus's usage, and
        # its counters lag behind writes until synced. A quota below that half-applies and leaves the
        # instance unable to write, even to raise the quota again.
        out = run(["btrfs", "qgroup", "show", "--sync", "--raw", "-f",
                   f"/var/lib/incus/storage-pools/{render.POOL}/containers/{name}"]).stdout
        used = int(out.splitlines()[-1].split()[1])   # the Referenced column
        needed = math.ceil(used / 2**30 + 1)   # and a GiB of room to work in
        if disk_gb < needed:
            raise CommandError(f"{name} uses {used / 2**30:.1f} GiB of disk; give it at least {needed} GiB")
    config = ([f"limits.cpu={cpu}"] if cpu else []) + ([f"limits.memory={memory_mb}MiB"] if memory_mb else [])
    if config:
        run(["incus", "config", "set", name, *config])
    if disk_gb:
        # The root disk is the profile's until the instance gets its own copy of it.
        action = "set" if "root" in devices(name) else "override"
        run(["incus", "config", "device", action, name, "root", f"size={disk_gb}GiB"])


def ensure_container(s: Settings, inst: Instance) -> None:
    """The instance's container from the plain Debian image (or moved here), running, with its network,
    the read-only policy and the Webapp published on the host's loopback (each step repeatable)."""
    if container_state(inst.name) is None:
        run(["incus", "init", IMAGE, inst.name, "--profile", render.PROFILE], timeout=1800)
    ensure_device(inst.name, "eth0", "nic", "nictype=routed", f"ipv4.address={inst.address}",
                  f"host_name=thinny-{inst.port}")
    ensure_policy(s, inst)
    ensure_device(inst.name, "webapp", "proxy", f"listen=tcp:127.0.0.1:{inst.port}",
                  f"connect=tcp:127.0.0.1:{inst.port}")
    ensure_trace(inst.name)
    if inst.moved:  # its old server turned autostart off for the move; the profile's applies again
        run(["incus", "config", "unset", inst.name, "boot.autostart"], check=False)  # 1 when unset already
    if container_state(inst.name) != "RUNNING":
        run(["incus", "start", inst.name])
    if inst.moved and s.model_lock:
        # At once: its gateway starts under the lock with the container, and Hermes would leave the model it
        # had on its old server as invalid YAML (see apply). The lock refuses the unset, so without the
        # managed scope.
        for key in starting_model(s):
            as_user(inst.name, ["sh", "-c", 'd=$(mktemp -d) && HERMES_MANAGED_DIR="$d" hermes config unset "$1"; '
                                'rmdir "$d"', "sh", key], check=False)  # 1 when not set
    in_container(inst.name, ["systemctl", "is-system-running", "--wait"], check=False, timeout=300)
    in_container(inst.name, ["sh", "-c", "cat > /etc/systemd/network/eth0.network && systemctl restart systemd-networkd"],
                 input=render.network_file(inst.address))


def ensure_trace(name: str) -> None:
    """The instance's trace of admins' access (trace.py), read-only in its container. Not required: without
    its directory the container still starts, and an import of it still works."""
    path = trace.DIR / name
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o755)   # for its account too, whatever the umask
    ensure_device(name, "trace", "disk", f"source={path}", f"path={trace.IN_CONTAINER}", *READ_ONLY, "required=false")


def ensure_packages(name: str) -> None:
    # Without a network apt does not fail, it retries for a very long time: find out first.
    for _ in range(3):
        if in_container(name, ["timeout", "10", "getent", "hosts", "deb.debian.org"], check=False).returncode == 0:
            break
        time.sleep(3)
    else:
        raise CommandError(f"{name}'s container cannot reach the internet. Does your firewall's forward chain "
                           "let it through? See README, Prerequisites.")
    in_container(name, ["sh", "-c", 'export DEBIAN_FRONTEND=noninteractive && apt-get update -q --error-on=any && '
                        'apt-get install -y -q --no-install-recommends "$@" && apt-get clean',
                        "sh", *render.PACKAGES], timeout=3600)


def ensure_account(name: str) -> None:
    """The instance's account in its container: passwordless sudo, user manager kept running."""
    if in_container(name, ["id", "-u", name], check=False).returncode != 0:
        in_container(name, ["useradd", "--create-home", "--shell", "/bin/bash", "--uid", str(INSTANCE_UID),
                            "--groups", "sudo", name])
    # Docker's group means root in the container, which sudo gives them anyway. Added before their user
    # manager starts: it keeps the groups it started with.
    in_container(name, ["usermod", "--append", "--groups", "docker", name])
    in_container(name, ["sh", "-c", "cat > /etc/sudoers.d/thinny && chmod 440 /etc/sudoers.d/thinny"],
                 input=f"{name} ALL=(ALL) NOPASSWD:ALL\n")
    in_container(name, ["loginctl", "enable-linger", name])
    bus = f"/run/user/{INSTANCE_UID}/bus"
    for _ in range(60):
        if in_container(name, ["test", "-S", bus], check=False).returncode == 0:
            return
        time.sleep(1)
    raise CommandError(f"the user manager of {name} did not start")


def install_hermes(s: Settings, name: str, commit: str, enable: bool) -> None:
    """Hermes at exactly `commit` with the official installer (a first install or an update),
    the Webapp, the Claude Code CLI, the language pack and the built-in plugins. `enable` turns the
    plugins on in the instance's own list."""
    with step("  official installer"):
        write_user_file(name, "/tmp/hermes-install.sh", host.installer())
        as_user(name, ["bash", "/tmp/hermes-install.sh", "--channel", "stable", "--commit", commit,
                       "--non-interactive"], timeout=3600)
    with step("  building the Webapp"):
        hermes(name, ["webapp", "--build-only"], timeout=3600)  # the installer does not build it
    with step("  Claude Code"):
        as_user(name, ["bash", "-c", f"set -o pipefail; command -v claude >/dev/null || curl -fsSL {CLAUDE_INSTALLER} | bash"],
                timeout=900)
        as_user(name, ["claude", "--version"], timeout=60)
    # The phone plugin's Python packages, as an extra Hermes keeps through every rebuild of its
    # environment. A plugin's own packages follow the instance's own list of plugins that are on, from
    # which Hermes drops the plugins the model lock turns on whenever it saves its config.
    with step("  phone plugin packages"):
        hermes(name, ["pm", "install", "--extra", "piper"], timeout=1800)
    plugins = ([(s.language_pack,)] if s.language_pack else []) + [args for _, args in render.PLUGINS]
    with step("  plugins"):
        for args in plugins:
            hermes(name, ["plugins", "install", *args, "--force", "--yes-deps", "--enable" if enable else "--no-enable"],
                   timeout=1800)
        hermes(name, ["pm", "gc"], check=False, timeout=600)


def installed_commit(name: str) -> str:
    """The commit of the instance's Hermes, as its container reports it."""
    return as_user(name, ["git", "-C", str(home(name) / "hermes-agent"), "rev-parse", "HEAD"],
                   check=False).stdout.strip()


def ensure_model_guard(name: str) -> None:
    """The server's model guard as a bundled plugin, read-only. Hidden from the checkout's git first:
    the installer stashes untracked files, which it cannot do with a read-only mount."""
    as_user(name, ["sh", "-c", 'grep -qx /plugins/model-guard/ "$1" || echo /plugins/model-guard/ >> "$1"', "sh",
                   str(home(name) / "hermes-agent/.git/info/exclude")])
    ensure_device(name, "model-guard", "disk", f"source={host.FILES / 'model-guard'}",
                  f"path={home(name) / 'hermes-agent/plugins/model-guard'}", *READ_ONLY)


def webapp_settings(name: str) -> dict[str, str]:
    """The instance's `webapp:` settings as text (`hermes config get webapp`)."""
    out = hermes(name, ["config", "get", "webapp"], timeout=600).stdout
    current = {}
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            current[key.strip()] = value.strip().strip("'\"")
    current["enabled"] = current.get("enabled", "").lower()
    return current


def configure_webapp(s: Settings, inst: Instance) -> bool:
    """Webapp settings in the instance's config; True when something changed."""
    wanted = {"enabled": "true", "host": "127.0.0.1", "port": str(inst.port), "public_url": inst.url}
    current = webapp_settings(inst.name)
    changes = {key: value for key, value in wanted.items() if current.get(key) != value}
    if changes:
        mark_restart([inst.name])
    for key, value in changes.items():
        hermes(inst.name, ["config", "set", f"webapp.{key}", value])
    return bool(changes)


def _env_lines(name: str) -> list[str]:
    return read_user_file(name, str(home(name) / ".env")).splitlines()


def ensure_sign_in(name: str) -> str | None:
    """Webapp username and password in the instance's .env; returns a new password, or None
    when it already has one."""
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
    with the newest Python environment of the instance's Hermes."""
    as_user(name, ["sh", "-c", 'py=$(ls -t "$2"/installs/*/environments/*/venv/bin/python3 | head -n 1) && '
                   'exec "$py" -I -c "$1" "$2"', "sh", SEED_UI, str(home(name))],
            input=json.dumps(render.ui_preferences(s)))


def check_gateway(s: Settings, inst: Instance, timeout: float = 180) -> None:
    """Gateway active, Webapp answering, model-guard enabled with the model lock; raise otherwise."""
    deadline = time.time() + timeout
    while True:
        active = gateway_active(inst.name)
        code = http_status(f"http://127.0.0.1:{inst.port}/")
        if active and code[0] in "23":
            break
        if time.time() > deadline:
            raise CommandError(f"{inst.name}: gateway {'active' if active else 'not active'}, Webapp HTTP {code}")
        time.sleep(5)
    if not settings_for(s, inst).model_lock:
        return
    if plugin_states(inst.name).get("model-guard") != "enabled":
        raise CommandError(f"{inst.name}: model-guard is not enabled")


def plugin_states(name: str) -> dict[str, str]:
    """The instance's plugins: name -> enabled, not enabled or disabled."""
    rows = json.loads(hermes(name, ["plugins", "list", "--json"], timeout=600).stdout)
    return {row["name"]: row["status"] for row in rows}


def start_or_restart_gateway(s: Settings, inst: Instance) -> None:
    _gateway_up(s, inst)
    _set_pending([n for n in _pending() if n != inst.name])


def _gateway_up(s: Settings, inst: Instance) -> None:
    unit = home(inst.name).parent / ".config/systemd/user/hermes-gateway.service"
    if as_user(inst.name, ["test", "-e", str(unit)], check=False).returncode == 0:
        hermes(inst.name, ["gateway", "restart", "--yes"], timeout=RESTART_TIMEOUT)
    else:
        hermes(inst.name, ["gateway", "install"], timeout=600)
    check_gateway(s, inst)


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


def restart_all(s: Settings, instances: list[Instance], reg: Registry) -> None:
    """Restart these gateways and every remembered one, one at a time (each waits for a
    running reply); stop at the first failure, leaving the rest remembered."""
    mark_restart([inst.name for inst in instances])
    for name in _pending():
        inst = reg.instances.get(name)
        if inst is None or inst.state != "active":
            _set_pending([n for n in _pending() if n != name])
            continue
        log(f"restarting {inst.name} (port {inst.port})")
        start_or_restart_gateway(s, inst)
        log(f"  ok {inst.name}")


# ---------- commands ----------

def add(s: Settings, reg: Registry, name: str, access: str | None, url: str = "", *, cpu: int | None = None,
        memory_mb: int | None = None, disk_gb: int | None = None, just_imported: bool = False) -> None:
    """Add an instance, or finish one whose add stopped. `just_imported`: it was moved here a moment ago, with
    the Hermes release this server runs."""
    access = access or s.access_default
    check_access(s, reg, name, access, url)
    if name in reg.instances:
        inst = reg.get(name)
        if inst.state == "active":
            raise CommandError(f"{name} already exists (use: thinny instance access {name} ...)")
        if inst.state == "disabled":
            raise CommandError(f"{name} was disabled; names are not reused")
        log(f"resuming the unfinished setup of {name}")
    else:
        check_free_name(name)
        inst = reg.add(name, access, s.port_range)
    inst.access, inst.url = access, url if access == "url" else ""
    reg.save()

    with step(f"{name}: container {inst.address}"):
        ensure_container(s, inst)
        set_limits(name, cpu, memory_mb, disk_gb)
    with step(f"{name}: system packages"):
        ensure_packages(name)
        ensure_account(name)
    tag, commit = host.release() or host.first_release()
    # A resumed add repeats it: it may have stopped halfway, or an update here changed the release. A moved
    # instance gets it like an update: only new built-ins are turned on.
    new_built_ins = inst.built_ins != render.BUILT_INS
    if not just_imported or new_built_ins:
        log(f"{name}: Hermes {tag}")
        install_hermes(s, name, commit, enable=not inst.moved or new_built_ins)
    with step(f"{name}: settings"):
        ensure_model_guard(name)
        inst.url = url_for(s, inst)
        reg.save()
        configure_webapp(s, inst)
        if not inst.moved:  # a moved instance keeps its users' choices
            hermes(name, ["config", "set", "display.language", s.language])
            if not s.model_lock:  # with the lock the policy sets them
                for key, value in starting_model(s).items():
                    hermes(name, ["config", "set", key, value])
            seed_ui(s, name)
        password = ensure_sign_in(name)
    host.reconcile_access(s, reg)  # outside a step: it logs when it installs Caddy
    with step(f"{name}: gateway"):
        start_or_restart_gateway(s, inst)
    inst.state, inst.built_ins = "active", render.BUILT_INS
    reg.save()

    print(f"\n{name} is ready.\n  Webapp:   {inst.url}\n  Username: {name}")
    print(f"  Password: {password}   (shown once; reset with: thinny instance reset-password {name})" if password
          else "  Password: unchanged (reset with: thinny instance reset-password " + name + ")")
    _print_access_hint(s, inst)
    if inst.moved:
        print(f"Next: if {name} did not use {s.model_provider} on its old server, sign it in: "
              f"thinny instance login {name}")
    else:
        print(f"Next: sign {name} in to {s.model_provider} before handing over: thinny instance login {name}")


def check_access(s: Settings, reg: Registry, name: str, access: str, url: str) -> None:
    """Refuse an access method this server cannot give the instance."""
    if access not in ACCESS_METHODS:
        raise CommandError(f"access must be one of {', '.join(ACCESS_METHODS)}")
    if access == "url":
        check_url(reg, name, url)
    if access == "lan" and not s.lan_address:
        raise CommandError("set access.lan_address in site.toml first")
    if access == "tailscale" and host.tailscale_status() is None:
        raise CommandError("Tailscale is not running on this server")


def check_free_name(name: str) -> None:
    """Refuse a name the host or Incus already uses."""
    if uid_of(name) is not None:  # Debian's system accounts, which the container has too
        raise CommandError(f"{name!r} is a system account name; choose another name")
    if container_state(name) is not None:
        raise CommandError(f"an Incus container {name!r} already exists; choose another name")


def starting_model(s: Settings) -> dict[str, str]:
    """Where an instance added without the model lock starts, in its own config."""
    return {"model.provider": s.model_provider, "model.default": s.model, "agent.reasoning_effort": s.reasoning_effort}


def check_url(reg: Registry, name: str, url: str) -> None:
    """An address no other instance uses: a host for one instance, or a host and one path each for
    several (with a tunnel port, Caddy routes by both)."""
    if not URL_RE.fullmatch(url):
        raise CommandError("url access needs an address like https://alice.example.org/ or "
                           "https://office.example.org/accounting/")
    parts = urlsplit(url)
    path = parts.path.strip("/")
    for other in reg.current():
        other_parts = urlsplit(other.url)
        other_path = other_parts.path.strip("/")
        if other.name != name and other.access == "url" and other_parts.hostname == parts.hostname \
                and (not path or not other_path or path == other_path):
            raise CommandError(f"{other.name} already uses {other.url}; a host has one instance on its root "
                               "or one on each path")


def _print_access_hint(s: Settings, inst: Instance) -> None:
    if inst.access == "url" and s.tunnel_port:
        print(f"  Caddy serves it to your tunnel at http://127.0.0.1:{s.tunnel_port} (see README, Access).")
    elif inst.access == "url":
        print(f"  Point your proxy or tunnel at http://127.0.0.1:{inst.port} (see README, Access).")
    elif inst.access == "local":
        print(f"  Open with: ssh -L {inst.port}:127.0.0.1:{inst.port} <you>@<server>, then browse to {inst.url}")
    elif inst.access == "lan":
        print(f"  Office PCs should trust Caddy's certificate: {host.OFFICE_CA} (see README).")


def change_access(s: Settings, reg: Registry, name: str, access: str, url: str = "") -> None:
    inst = _active(reg, name)
    check_access(s, reg, name, access, url)
    inst.access, inst.url = access, url if access == "url" else ""
    inst.url = url_for(s, inst)
    reg.save()
    configure_webapp(s, inst)
    host.reconcile_access(s, reg)
    start_or_restart_gateway(s, inst)
    print(f"{name}: {inst.url}")
    _print_access_hint(s, inst)


def limits(reg: Registry, name: str, cpu: int | None, memory_mb: int | None, disk_gb: int | None) -> None:
    _active(reg, name)
    if not (cpu or memory_mb or disk_gb):
        raise CommandError("give --cpu, --memory-mb or --disk-gb")
    set_limits(name, cpu, memory_mb, disk_gb)


def model_of(s: Settings, reg: Registry, name: str) -> tuple[str, str]:
    """The provider and model the instance uses: its lock's, or those in its own config."""
    s = settings_for(s, _active(reg, name))
    if s.model_lock:
        return s.model_provider, s.model
    provider, model = (hermes(name, ["config", "get", key], check=False).stdout.strip() or default
                       for key, default in (("model.provider", s.model_provider), ("model.default", s.model)))
    return provider, model


def lock(s: Settings, reg: Registry, name: str, models: list[str]) -> None:
    """Lock the instance to these models (or the one it uses) of the provider it uses, whatever the site's lock."""
    inst = _active(reg, name)
    provider, model = model_of(s, reg, name)
    inst.lock, inst.lock_provider = models or [model], provider
    reg.save()  # first: its config loses its model on the way, so a failure leaves the lock for apply to finish
    update_policy(s, inst)
    restart_all(s, [], reg)
    print(f"{name} is locked to {provider}: {', '.join(inst.lock)}")


def unlock(s: Settings, reg: Registry, name: str) -> None:
    """Let the instance's users choose their models, whatever the site's lock. It starts with the model it was
    locked to: the lock took the model out of its own config."""
    inst = _active(reg, name)
    locked = settings_for(s, inst)
    inst.lock, inst.lock_provider = [], ""
    update_policy(s, inst)
    if locked.model_lock:
        for key, value in starting_model(locked).items():
            hermes(name, ["config", "set", key, value])
    reg.save()  # last: until then, unlock again gives it its model
    restart_all(s, [], reg)
    print(f"{name} is unlocked: its users choose their models")


def login(reg: Registry, name: str, provider: str) -> None:
    _active(reg, name)
    if provider == CLAUDE_PROVIDER:
        print(f"Signing {name} in to a Claude subscription. Give the link below to whoever uses {name}, then "
              "enter the code they get back.")
        cmd = ["claude", "auth", "login"]
    else:
        print(f"Signing {name} in to {provider}. Give the code below to whoever uses {name}; it expires "
              "in about 15 minutes.")
        cmd = ["hermes", "auth", "add", provider, "--no-browser"]
    if as_user_interactive(name, cmd):
        raise CommandError("the login did not finish")


def password(s: Settings, reg: Registry, name: str) -> None:
    inst = _active(reg, name)
    new = reset_password(name)
    start_or_restart_gateway(s, inst)
    print(f"{name}: new Webapp password: {new}   (shown once; open browsers must sign in again)")


def disable(s: Settings, reg: Registry, name: str) -> None:
    """Disable an instance: its container stops for good; its data stays in it."""
    inst = reg.get(name)
    if inst.state == "disabled":
        print(f"{name} is already disabled")
        return
    if container_state(name) is not None:
        stop(name)
    inst.state = "disabled"
    reg.save()
    host.reconcile_access(s, reg)
    print(f"{name} disabled: container stopped, Webapp and agent with it. Its data stays in the container {name}.")


def stop(name: str) -> None:
    """Stop the instance's container and keep it stopped."""
    run(["incus", "config", "set", name, "boot.autostart=false"])  # first: a reboot must not bring it back
    if container_state(name) == "RUNNING" and \
            run(["incus", "stop", name, "--timeout", "60"], check=False, timeout=120).returncode != 0:
        run(["incus", "stop", name, "--force"])  # its root may refuse to shut down
    if container_state(name) == "RUNNING":
        raise CommandError(f"{name}'s container is still running")


def list_instances(reg: Registry) -> None:
    rows = [("NAME", "STATE", "ACCESS", "PORT", "GATEWAY", "URL")]
    for inst in sorted(reg.instances.values(), key=lambda inst: inst.port):
        gateway = "-" if inst.state == "disabled" or container_state(inst.name) is None else \
            ("active" if gateway_active(inst.name) else "INACTIVE")
        rows.append((inst.name, inst.state, inst.access, str(inst.port), gateway, inst.url))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _active(reg: Registry, name: str) -> Instance:
    inst = reg.get(name)
    if inst.state != "active":
        raise RegistryError(f"{name} is {inst.state}")
    return inst
