"""Adding, changing and removing instances. Each instance has its own Incus container; everything in
its home runs as the instance's account, inside it."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sys
import textwrap
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import host, purelymail, render, trace
from .registry import Instance, Registry, RegistryError, validate_name
from .settings import ACCESS_METHODS, Settings, group_file, secret
from .system import (INSTANCE_UID, STATE_DIR, CommandError, as_user, as_user_cmd, as_user_interactive, gateway_active,
                     http_status, in_container, log, printable, read_user_file, run, step, tailscale_name, uid_of,
                     write_file, write_user_file)

AUTH_PREFIX = "HERMES_DASHBOARD_BASIC_AUTH_"
RESTART_TIMEOUT = 45 * 60  # a restart waits for a running reply (up to 30 min by default)
RESTART_PENDING = STATE_DIR / "restart-pending"
IMAGE = "images:debian/13"
URL_RE = re.compile(r"https?://[a-z0-9.-]+(:\d+)?/?")
CLAUDE_INSTALLER = "https://claude.ai/install.sh"
# The email client of Hermes's himalaya skill, for a mailbox. v2 dropped the commands the skill uses (template,
# MML, --output). Its releases publish no checksums; these are GitHub's for the release's files.
HIMALAYA = "v1.2.0"
HIMALAYA_SHA256 = {"x86_64": "e04e6382e3e664ef34b01afa1a2216113194a2975d2859727647b22d9b36d4e4",
                   "aarch64": "643020b220991fac67726f3be11310fcf806e757feadbbab3efbddd713597872"}
HIMALAYA_BIN = "/usr/local/bin/himalaya"
WEBUI_GIT = f"https://github.com/{host.WEBUI_REPO}"
FRONTEND_NAMES = {"webapp": "Webapp", "webui": "WebUI"}
CGROUPS = Path("/sys/fs/cgroup")   # each container's as lxc.payload.<name>
# The Claude subscription plugin's provider; it signs in through Claude Code.
CLAUDE_PROVIDER = "claude-subscription-directsdk-experimental"
PROVIDER_NAMES = {"openai-codex": "ChatGPT", CLAUDE_PROVIDER: "Claude"}
# The labels of the windows in `hermes usage --json`: Codex's, then the Claude subscription plugin's.
FIVE_HOURS, WEEK = ("Session", "Current session"), ("Weekly", "Current week")
USAGE_TIMEOUT = 20   # per command; the plugin asks Claude Code, which may take 10 s
# Host folders in a container. Incus does not lock a disk added to a running container read-only, and with
# shift its root could remount it and write into the folder as the host's root. Without shift, it is just
# another account there. "shift=false", not left out, so older disks lose their shift too.
READ_ONLY = ("readonly=true", "shift=false")
# The phone's voice and what it says itself on a server in Czech (else the phone plugin's English and Hermes's
# English voice), from the plugin's README.
PHONE_CZECH = {"tts.piper.voice": "cs_CZ-jirka-medium"} | {
    f"plugins.entries.hermes-3310.settings.{key}": value for key, value in {
        "language": "cs",
        "instructions": "Odpovídej česky, mluveným stylem.",
        "phrase_pin": "Zadej kód a mřížku.",
        "phrase_pin_spoken": "Zadej kód a mřížku, nebo ho řekni.",
        "phrase_wrong_pin": "Špatný kód.",
        "phrase_hello": "Ahoj, tady Hermes. Co potřebuješ?",
        "phrase_bye": "Na shledanou.",
        "phrase_not_understood": "Nerozuměl jsem, zopakuj to prosím.",
        "phrase_stopped": "Zastaveno.",
        "phrase_denied": "Tohle potřebuje schválení, to po telefonu nejde.",
        "phrase_approval": "Potřebuju tvoje schválení.",
        "phrase_approval_addresses": "Adresy v tom:",
        "phrase_approval_dangerous": "Bezpečnostní kontrola to považuje za nebezpečné.",
        "phrase_approval_keys": "Jedničkou to jednou povolíš, dvojkou odmítneš.",
        "phrase_approved": "Povoleno.",
        "phrase_refused": "Odmítnuto.",
        "phrase_missed": "Zkoušel jsem ti volat.",
    }.items()}


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


def webui_files(name: str) -> dict[str, PurePosixPath]:
    """Where hermes-webui lives in the instance's container: its checkout, unit, password and state."""
    user = PurePosixPath(f"/home/{name}")
    return {"checkout": user / "hermes-webui", "unit": user / ".config/systemd/user/hermes-webui.service",
            "password": user / ".config/hermes-webui.env", "state": home(name) / "webui"}


def url_for(s: Settings, inst: Instance) -> str:
    ts = tailscale_name() if inst.access == "tailscale" else ""
    return render.instance_url(s, inst.name, inst.access, inst.port, url=inst.url, tailscale_name=ts)


# ---------- pieces ----------

def container_state(name: str) -> str | None:
    """RUNNING, STOPPED, ... or None when there is no such container."""
    out = run(["incus", "list", "--format", "csv", "--columns", "s", f"^{name}$"]).stdout.strip()
    return out or None


def container_states() -> dict[str, str]:
    """Every container's state (RUNNING, STOPPED, ...) by its name, in one incus list."""
    out = run(["incus", "list", "--format", "csv", "--columns", "ns"]).stdout
    return dict(line.split(",", 1) for line in out.splitlines() if line)


def devices(name: str) -> dict[str, dict[str, str]]:
    """The container's own devices (not its profile's): name -> properties."""
    return json.loads(run(["incus", "query", f"/1.0/instances/{name}"]).stdout)["devices"]


def settings_for(s: Settings, inst: Instance) -> Settings:
    """The settings with the Hermes settings of the instance's group and its mailbox, and its own model lock, when it
    has them."""
    if inst.group:
        if inst.group not in s.groups:
            raise CommandError(f"{inst.name} is in the group {inst.group}, but {group_file(inst.group)} is missing")
        s = dataclasses.replace(s, hermes=s.groups[inst.group])
    if inst.mailbox:
        groups = s.hermes.get("agent", {}).get("environment_hint")
        hint = "\n\n".join(filter(None, [groups, render.mail_hint(inst.mailbox, HIMALAYA_BIN)]))
        s = dataclasses.replace(s, hermes=render.merged(s.hermes, {"agent": {"environment_hint": hint}}))
    if inst.lock is None:
        return s
    if not inst.lock:
        return dataclasses.replace(s, model_lock=False)
    return dataclasses.replace(s, model_lock=True, model_provider=inst.lock_provider, model=inst.lock[0],
                               allowed_models=tuple(inst.lock))


def update_policy(s: Settings, inst: Instance) -> None:
    """The instance's policy as the settings and its own lock say; a change restarts its gateway. One that unlocks
    it leaves it on the model it was locked to, in its own config (unless it has one), for its users to change."""
    s = settings_for(s, inst)
    kept = host.POLICIES / inst.name / "starting-model.json"   # the lock's model, until its own config has it
    if not host.policy_current(s, inst.name):
        if inst.state == "active":
            log(f"{inst.name}: its policy changes: its gateway restarts")
            mark_restart([inst.name])  # before the change, so a failure keeps the intent
            if not s.model_lock and (model := locked_model(inst.name)):
                write_file(kept, json.dumps(model))   # before the policy, its only record, changes
        # Before the policy pins them: Hermes drops pinned keys from an instance's config whenever it saves it,
        # writes a `model:` section left empty as invalid YAML (v0.23.1), and refuses to unset them once pinned.
        # Pending instances too: a resumed add would start its gateway under the policy.
        for key in pinned_keys(s):
            hermes(inst.name, ["config", "unset", key], check=False)  # 1 when not set
    ensure_policy(s, inst)
    if kept.exists():   # also after a failure here: apply finishes it
        # Unless it has a model of its own; Hermes shows a group's too, which leaves it the lock's provider and effort.
        if "model.default" in pinned_keys(s) or \
                not hermes(inst.name, ["config", "get", "model.default"], check=False).stdout.strip():
            set_config(s, inst.name, json.loads(kept.read_text(encoding="utf-8")))
        kept.unlink()


def locked_model(name: str) -> dict[str, str]:
    """The model the instance's policy locks it to, as starting_model has it ({}: none), read back from the lines
    render.to_yaml wrote. model.default last: until it is set, a repeat sets the others again."""
    path = host.POLICIES / name / "config.yaml"
    values, section = {}, ""
    decode = json.JSONDecoder().raw_decode
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        if line.startswith('"'):
            section = decode(line)[0]
        elif line.startswith('  "'):
            key, end = decode(line, 2)
            values[f"{section}.{key}"] = line[end + 1:]   # its value as JSON, if any
    if "model-guard" not in json.loads(values.get("plugins.enabled", "[]")):   # the lock's own plugin
        return {}
    return {key: json.loads(values[key]) for key in ("model.provider", "agent.reasoning_effort", "model.default")}


def pinned_keys(s: Settings) -> list[str]:
    """The keys of the instance's own config its policy pins: the model lock's, and its group's (each a
    dotted key, as Hermes counts them)."""
    keys = list(starting_model(s)) if s.model_lock else []

    def walk(config: dict, prefix: str) -> None:
        for key, value in config.items():
            if isinstance(value, dict) and value:
                walk(value, f"{prefix}{key}.")
            else:
                keys.append(prefix + key)
    walk(s.hermes, "")
    return keys


def set_config(s: Settings, name: str, values: dict[str, str]) -> None:
    """These values in the instance's own config, but those its policy pins: Hermes refuses them."""
    pinned = pinned_keys(s)
    for key, value in values.items():
        if key not in pinned:
            hermes(name, ["config", "set", key, value])


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


def disk_used(name: str, sync: bool = False) -> int:
    """Bytes the instance's disk quota counts. btrfs's quota counts data shared with the image or a
    snapshot too, unlike Incus's usage, and its counters lag behind writes until synced."""
    out = run(["btrfs", "qgroup", "show", *(["--sync"] if sync else []), "--raw", "-f",
               f"/var/lib/incus/storage-pools/{render.POOL}/containers/{name}"]).stdout
    return int(out.splitlines()[-1].split()[1])   # the Referenced column


def set_limits(name: str, cpu: int | None, memory_mb: int | None, disk_gb: int | None) -> None:
    """The instance's own limits instead of the profile's (config.toml's [container]); None keeps
    the current one. A running container gets them at once."""
    if disk_gb:
        # A quota below what it counts half-applies and leaves the instance unable to write, even to
        # raise the quota again.
        used = disk_used(name, sync=True)
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


def ensure_packages(name: str, resume: str) -> None:
    """`resume`: the thinny command that goes on where this one stopped."""
    # Without a network apt does not fail, it retries for a very long time: find out first.
    for _ in range(3):
        if in_container(name, ["timeout", "10", "getent", "hosts", "deb.debian.org"], check=False).returncode == 0:
            break
        time.sleep(3)
    else:
        if host.ufw_lacks_forward():
            sudo = "sudo " if os.environ.get("SUDO_USER") else ""
            raise CommandError(f"ufw blocks the containers' traffic, so {name}'s container cannot reach the internet "
                               f"(see README, Prerequisites). Run: {sudo}{host.UFW_FORWARD}\n"
                               f"Then run {resume} again to resume.")
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


def ensure_mail_client(inst: Instance) -> None:
    """For an instance with a mailbox: the pinned himalaya, which its agent uses through Hermes's himalaya skill, with
    the mailbox as an account named after its address. A user's own himalaya accounts stay."""
    if not inst.mailbox:
        return
    name = inst.name
    if not in_container(name, [HIMALAYA_BIN, "--version"], check=False).stdout.startswith(f"himalaya {HIMALAYA} "):
        arch = in_container(name, ["uname", "-m"]).stdout.strip()
        if arch not in HIMALAYA_SHA256:
            raise CommandError(f"{name}: himalaya has no release for {arch}")
        url = f"https://github.com/pimalaya/himalaya/releases/download/{HIMALAYA}/himalaya.{arch}-linux.tgz"
        in_container(name, ["sh", "-c", 'set -e; d=$(mktemp -d); curl -fsSL --max-time 300 -o "$d/h.tgz" "$1"; '
                            'echo "$2  $d/h.tgz" | sha256sum -c --quiet; tar -xzf "$d/h.tgz" -C "$d" himalaya; '
                            'install -m 755 "$d/himalaya" "$3"; rm -r "$d"', "sh", url, HIMALAYA_SHA256[arch],
                            HIMALAYA_BIN], timeout=600)
    path = f"/home/{name}/.config/himalaya/config.toml"
    config = read_user_file(name, path)
    account = render.himalaya_account(inst.mailbox, str(home(name) / ".env"), default=not config.strip())
    try:
        if inst.mailbox not in tomllib.loads(config).get("accounts", {}):
            tomllib.loads(config + account)   # fails e.g. after `accounts = {...}`
            as_user(name, ["mkdir", "-p", str(PurePosixPath(path).parent)])
            write_user_file(name, path, config + account)
    except tomllib.TOMLDecodeError as e:
        log(f"{name}: {path} gets no account for {inst.mailbox}, which its agent sends mail with: {e}")


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


def install_webui(name: str, commit: str = "") -> None:
    """hermes-webui at exactly `commit`, by default at the release the instances that use it run (the first one
    to: the latest stable). A first install or an update, also back from where its own updater took it."""
    if not commit:
        _, commit = host.release(host.WEBUI_RELEASE_FILE) or host.first_release(host.WEBUI_REPO,
                                                                                 host.WEBUI_RELEASE_FILE)
    as_user(name, ["sh", "-c", 'git init -q "$1" && cd "$1" && git fetch -q --depth 1 "$2" "$3" && '
                   'git checkout -q --force FETCH_HEAD', "sh", str(webui_files(name)["checkout"]), WEBUI_GIT, commit],
            timeout=900)


def installed_commit(name: str, webui: bool = False) -> str:
    """The commit of the instance's Hermes (or WebUI), as its container reports it."""
    checkout = webui_files(name)["checkout"] if webui else home(name) / "hermes-agent"
    return as_user(name, ["git", "-C", str(checkout), "rev-parse", "HEAD"], check=False).stdout.strip()


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
    """The instance's frontend: the Webapp's settings in its config, on, or off for WebUI on its port with
    WebUI's unit; True when something changed. WebUI starts in the server's look."""
    webui = inst.frontend == "webui"
    wanted = {"enabled": str(not webui).lower(), "host": "127.0.0.1", "port": str(inst.port), "public_url": inst.url}
    current = webapp_settings(inst.name)
    changes = {key: value for key, value in wanted.items() if current.get(key) != value}
    files = webui_files(inst.name)
    unit = render.webui_unit(inst.name, inst.port, inst.url.startswith("https://")) if webui else ""
    unit_changed = read_user_file(inst.name, str(files["unit"])) != unit
    if changes or unit_changed:
        mark_restart([inst.name])
    for key, value in changes.items():
        hermes(inst.name, ["config", "set", f"webapp.{key}", value])
    if unit_changed and webui:
        as_user(inst.name, ["mkdir", "-p", str(files["unit"].parent)])
        write_user_file(inst.name, str(files["unit"]), unit)
    elif unit_changed:   # back to the Webapp: WebUI leaves its port, and does not start with the container
        as_user(inst.name, ["systemctl", "--user", "disable", "--now", "hermes-webui"], timeout=RESTART_TIMEOUT)
        as_user(inst.name, ["rm", "-f", str(files["unit"])])
    if unit_changed:
        as_user(inst.name, ["systemctl", "--user", "daemon-reload"])
    if webui:
        as_user(inst.name, ["systemctl", "--user", "enable", "hermes-webui"])
        settings = files["state"] / "settings.json"
        if not read_user_file(inst.name, str(settings)):
            as_user(inst.name, ["mkdir", "-p", str(files["state"])])
            write_user_file(inst.name, str(settings), json.dumps(render.webui_settings(s), indent=1) + "\n")
    return bool(changes) or unit_changed


def _env_lines(name: str) -> list[str]:
    return read_user_file(name, str(home(name) / ".env")).splitlines()


def env_values(name: str) -> dict[str, str]:
    """The instance's .env, the values set_env wrote unquoted, others as they are."""
    values = {}
    for key, _, value in (line.partition("=") for line in _env_lines(name)):
        try:
            values[key] = json.loads(value) if value.startswith('"') else value
        except ValueError:
            values[key] = value
    return values


def set_env(name: str, values: dict[str, str]) -> None:
    """`values` in the instance's .env, in place of what it had for their keys."""
    lines = [line for line in _env_lines(name) if line.partition("=")[0] not in values]
    # Double-quoted with \ and " escaped: Hermes (python-dotenv) and the phone plugin's install script both read
    # a password with $, #, quotes or backslashes so. In single quotes, a closing backslash breaks dotenv.
    write_user_file(name, str(home(name) / ".env"), "\n".join(
        lines + [f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items()]) + "\n")


def ensure_sign_in(inst: Instance) -> str | None:
    """The sign-in of the instance's frontend; returns a new password, or None when it already has one. An
    instance from before WebUI has none for WebUI: its Webapp's is kept as a hash only."""
    has = any(line.startswith(AUTH_PREFIX + "USERNAME=") for line in _env_lines(inst.name)) and (
        inst.frontend == "webapp" or read_user_file(inst.name, str(webui_files(inst.name)["password"])))
    return None if has else reset_password(inst.name)


def reset_password(name: str, password: str | None = None) -> str:
    """A new password for both frontends (default: a random one): the Webapp's username and password in the
    instance's .env, and WebUI's. WebUI must not run: it would keep the sessions it signs everyone out of."""
    password = password or secrets.token_urlsafe(12)
    lines = [line for line in _env_lines(name) if not line.startswith(AUTH_PREFIX)] + [
        f"{AUTH_PREFIX}USERNAME={name}",
        f"{AUTH_PREFIX}PASSWORD_HASH='{password_hash(password)}'",  # quoted: '$' in the hash
        f"{AUTH_PREFIX}SECRET={secrets.token_urlsafe(32)}"]
    write_user_file(name, str(home(name) / ".env"), "\n".join(lines) + "\n")
    # In plain text: WebUI takes no hash from its environment. Its unit reads the file, the gateway does not.
    files = webui_files(name)
    as_user(name, ["mkdir", "-p", str(files["password"].parent)])
    # In double quotes with \ and " escaped: systemd then takes a typed one's quotes, $ and # as they are.
    quoted = password.replace("\\", "\\\\").replace('"', '\\"')
    write_user_file(name, str(files["password"]), f'HERMES_WEBUI_PASSWORD="{quoted}"\n')
    as_user(name, ["rm", "-f", str(files["state"] / ".sessions.json")])
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
    """Webapp look from the settings, through Hermes's own store (same lock and revision bump)."""
    hermes_python(name, SEED_UI, input=json.dumps(render.ui_preferences(s)))


def hermes_python(name: str, script: str, **kw):
    return run(hermes_python_cmd(name, script), **kw)


def hermes_python_cmd(name: str, script: str) -> list[str]:
    """`script` run by the newest Python environment of the instance's Hermes, as its account, with its Hermes home as
    argv[1]."""
    return as_user_cmd(name, ["sh", "-c", 'py=$(ls -t "$2"/installs/*/environments/*/venv/bin/python3 | head -n 1) && '
                              'exec "$py" -I -c "$1" "$2"', "sh", script, str(home(name))])


# Hermes's default model for the provider on stdin (what its Telegram chats fall back on; its Webapp's need one set),
# or, as it has none for the Claude plugin, the plugin's first model.
DEFAULT_MODEL = r'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "hermes-agent"))
from hermes_cli.models import get_default_model_for_provider
from providers import get_provider_profile
provider = sys.stdin.read()
profile = get_provider_profile(provider)
model = get_default_model_for_provider(provider)
print(model or (profile.fallback_models[0] if profile and profile.fallback_models else ""))
'''


def hermes_default_model(name: str, provider: str) -> str:
    res = hermes_python(name, DEFAULT_MODEL, input=provider, check=False, timeout=120)
    model = printable((res.stdout.splitlines() or [""])[-1].strip())   # after anything Hermes says on the way
    if res.returncode or not model:
        error = (res.stderr.strip().splitlines() or [f"exit {res.returncode}"])[-1]
        raise CommandError(f"{name}'s Hermes gave no default model for {provider} ({printable(error)}). "
                           f"Fix the cause, then run thinny instance add {name} again to resume.")
    return model


# The models the instance can use, by provider, as the Webapp's model picker offers them (hermes_cli/inventory.py):
# Hermes's cache or built-in lists, without asking the providers. Only signed-in providers: Hermes also lists the
# one in use after its sign-in is gone, and a provider run by another program (the Claude plugin's) whenever the
# program is there, so that program's own sign-in check decides, as in `hermes model`.
MODELS = r'''
import json, sys
from pathlib import Path
home = Path(sys.argv[1])
sys.path.insert(0, str(home / "hermes-agent"))
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_cli.inventory import build_models_payload, load_picker_context
from providers import get_provider_profile


def signed_in(row):
    profile = get_provider_profile(row["slug"])
    status = profile.setup_status() if profile else None
    return row.get("authenticated") is not False and (status is None or status["logged_in"])


load_hermes_dotenv()   # its keys and settings in .env, as `hermes` starts
rows = build_models_payload(load_picker_context(), explicit_only=True, for_picker=True, non_blocking_catalogs=True,
                            probe_custom_providers=False)["providers"]
json.dump({row["slug"]: row["models"] for row in rows if signed_in(row)}, sys.stdout)
'''


def available_models(name: str) -> dict[str, list[str]]:
    """Provider -> the models the instance can use (MODELS), without control characters."""
    res = hermes_python(name, MODELS, check=False, timeout=120)
    try:
        models = json.loads(res.stdout) if res.returncode == 0 else None
    except ValueError:
        models = None
    if isinstance(models, dict) and all(isinstance(ids, list) and all(isinstance(m, str) for m in ids)
                                        for ids in models.values()):
        return {printable(provider): [printable(m) for m in ids] for provider, ids in models.items()}
    # It failed, or its users made Hermes say something else.
    error = (res.stderr.strip().splitlines() or [f"exit {res.returncode}"])[-1]
    raise CommandError(f"could not get the models {name} can use from Hermes ({printable(error)})")


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
        time.sleep(1)
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
    if inst.frontend == "webui":   # after the gateway: also after a Hermes update, which WebUI needs
        as_user(inst.name, ["systemctl", "--user", "restart", "hermes-webui"], timeout=RESTART_TIMEOUT)
    check_gateway(s, inst)
    # Chats going on keep the prompt they started with: each rebuilds it once, on its next turn, with what changed
    # (e.g. the policy). Not worth a restart that counts as failed.
    try:
        hermes(inst.name, ["sessions", "refresh-prompts"], timeout=120)
    except CommandError as e:
        log(f"{inst.name}: chats going on keep their prompts: {e}")


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

def add(s: Settings, reg: Registry, name: str, access: str | None, url: str = "", *, frontend: str | None = None,
        cpu: int | None = None, memory_mb: int | None = None, disk_gb: int | None = None,
        phone: dict[str, str] | None = None, group: str | None = None, just_imported: bool = False) -> None:
    """Add an instance, or finish one whose add stopped. `frontend`: None keeps the one it has (a new instance:
    the server's). `phone`: the settings of a phone line to connect. `group`: None keeps the one it has (a new
    instance: none). `just_imported`: it was moved here a moment ago, with the Hermes release this server runs."""
    access = access or s.access_default
    check_access(s, reg, name, access, url)
    check_name(reg, name)
    if name in reg.instances:
        inst = reg.get(name)
        log(f"resuming the unfinished setup of {name}")
    else:
        inst = reg.add(name, access, s.port_range)
        inst.frontend = s.frontend
    inst.access, inst.url = access, url if access == "url" else ""
    inst.frontend = frontend or inst.frontend
    inst.group = inst.group if group is None else group
    s = settings_for(s, inst)
    reg.save()

    with step(f"{name}: container {inst.address}"):
        ensure_container(s, inst)
        set_limits(name, cpu, memory_mb, disk_gb)
    with step(f"{name}: system packages"):
        ensure_packages(name, f"thinny instance add {name}")
        ensure_account(name)
    tag, commit = host.release() or host.first_release()
    # A resumed add repeats it: it may have stopped halfway, or an update here changed the release. A moved
    # instance gets it like an update: only new built-ins are turned on.
    new_built_ins = inst.built_ins != render.BUILT_INS
    if not just_imported or new_built_ins:
        log(f"{name}: Hermes {tag}")
        install_hermes(s, name, commit, enable=not inst.moved or new_built_ins)
    if inst.frontend == "webui":   # a moved instance too: this server's release
        with step(f"{name}: WebUI"):
            install_webui(name)
    with step(f"{name}: settings"):
        ensure_model_guard(name)
        ensure_mail_client(inst)   # a moved instance's mailbox
        inst.url = url_for(s, inst)
        reg.save()
        configure_webapp(s, inst)
        if not inst.moved:  # a moved instance keeps its users' choices
            if not s.model_lock:   # else the lock's: its group's, or its Hermes's default for its provider
                s = dataclasses.replace(s, model=str(s.hermes["model"]["default"]) if "model.default" in pinned_keys(s)
                                        else hermes_default_model(name, s.model_provider))
            # Piper (the phone's voice) speaks slowly at its own speed; lower is faster.
            set_config(s, name, {"display.language": s.language, "tts.piper.length_scale": "0.65",
                                 **starting_model(s)})
            seed_ui(s, name)
        password = ensure_sign_in(inst)
    if phone:
        with step(f"{name}: phone line"):
            registered = connect_phone(s, name, phone)
        if not registered:
            log(f"{name}: the phone line did not register with {phone['SIP_SERVER']}. Check the SIP user name and "
                "password, and that the server's firewall (and its provider's) lets UDP from it in (see README).")
    host.reconcile_access(s, reg)  # outside a step: it logs when it installs Caddy
    with step(f"{name}: gateway"):
        start_or_restart_gateway(s, inst)
    inst.state, inst.built_ins = "active", render.BUILT_INS
    reg.save()

    print(f"\n{name} is ready.\n  {FRONTEND_NAMES[inst.frontend] + ':':<9} {inst.url}")
    if inst.frontend == "webapp":   # WebUI asks for the password only
        print(f"  Username: {name}")
    print(f"  Password: {password}   (shown once; reset with: thinny instance reset-password {name})" if password
          else "  Password: unchanged (reset with: thinny instance reset-password " + name + ")")
    _print_access_hint(s, inst)
    if inst.moved:
        print(f"Next: if {name} did not use {s.model_provider} on its old server, sign it in: "
              f"thinny instance login {name}")
    else:
        print(f"Next: sign {name} in to {s.model_provider} before handing over: thinny instance login {name}")
        if not s.model_lock:
            print(f"{name} starts on {s.model}" + (f", which its group sets: thinny config edit --group {inst.group}"
                  if "model.default" in pinned_keys(s) else
                  f". To pick another once it is signed in: thinny instance model {name} <model>"))
        if secret("THINNY_TELEGRAM_MANAGER_TOKEN"):
            print(f"Then send its user a Telegram bot: thinny instance telegram {name}")
        if s.email_domain:
            print(f"Then give it a mailbox, {name}@{s.email_domain}: thinny instance email {name} "
                  "<its user's address>")


def connect_phone(s: Settings, name: str, phone: dict[str, str]) -> bool:
    """The phone line in the instance's .env and config, and its Asterisk, as hermes-3310's README sets them
    up; the gateway gets them when it restarts. True when the line registered with its SIP server."""
    set_env(name, phone)
    config = {"platforms.phone.enabled": "true", "tts.provider": "piper", "tts.streaming.provider": "piper_stream",
              "stt.provider": "openai", "stt.openai.model": "gpt-transcribe"}
    set_config(s, name, config | (PHONE_CZECH if s.language == "cs" else {}))
    as_user(name, [str(home(name) / "plugins/hermes-3310/install-asterisk.sh")], timeout=1800)
    for _ in range(20):
        if "Registered" in as_user(name, ["docker", "exec", "hermes-3310-asterisk", "asterisk", "-rx",
                                          "pjsip show registrations"], check=False).stdout.split():
            return True
        time.sleep(3)
    return False


def check_access(s: Settings, reg: Registry, name: str, access: str, url: str) -> None:
    """Refuse an access method this server cannot give the instance."""
    if access not in ACCESS_METHODS:
        raise CommandError(f"access must be one of {', '.join(ACCESS_METHODS)}")
    if access == "url":
        check_url(reg, name, url)
    if access == "lan" and not s.lan_address:
        raise CommandError("set access.lan_address in config.toml first")
    if access == "tailscale" and host.tailscale_status() is None:
        raise CommandError("Tailscale is not running on this server")


def check_name(reg: Registry, name: str) -> None:
    """Refuse a name `add` cannot take. The name of an unfinished add passes: the add resumes."""
    inst = reg.instances.get(name)
    if inst is None:
        validate_name(name)
        check_free_name(name)
    elif inst.state == "active":
        raise CommandError(f"{name} already exists (use: thinny instance access {name} ...)")
    elif inst.state in ("disabled", "removed"):
        raise CommandError(f"{name} was {inst.state}; to reuse its name: thinny instance remove {name} --purge")


def check_free_name(name: str) -> None:
    """Refuse a name the host or Incus already uses."""
    if uid_of(name) is not None:  # Debian's system accounts, which the container has too
        raise CommandError(f"{name!r} is a system account name; choose another name")
    if container_state(name) is not None:
        raise CommandError(f"an Incus container {name!r} already exists; choose another name")


def starting_model(s: Settings) -> dict[str, str]:
    """Where an instance starts, in its own config: its lock's model, or when added, Hermes's default."""
    return {"model.provider": s.model_provider, "model.default": s.model, "agent.reasoning_effort": s.reasoning_effort}


def check_url(reg: Registry, name: str, url: str) -> None:
    """A host no other instance uses: instances on one host would share the browser's origin, so one's
    users could act as another's signed-in users."""
    if not URL_RE.fullmatch(url):
        raise CommandError("url access needs an address with a host of its own and no path, like "
                           "https://alice.example.org/")
    for other in reg.current():
        if other.name != name and other.access == "url" and urlsplit(other.url).hostname == urlsplit(url).hostname:
            raise CommandError(f"{other.name} already uses {other.url}; a host has one instance")


def _print_access_hint(s: Settings, inst: Instance) -> None:
    if inst.access == "url" and s.tunnel_port:
        print(f"  Caddy serves it to your tunnel at http://127.0.0.1:{s.tunnel_port} (see README, Access).")
    elif inst.access == "url":
        print(f"  Point your proxy or tunnel at http://127.0.0.1:{inst.port} (see README, Access).")
    elif inst.access == "local":
        print(f"  Open with: ssh -L {inst.port}:127.0.0.1:{inst.port} <you>@<server>, then browse to {inst.url}")
    elif inst.access == "lan":
        print("  Office PCs should trust Caddy's certificate: "
              f"http://{render.host_for_url(s.lan_address)}/office-root.crt (see README).")


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


def change_frontend(s: Settings, reg: Registry, name: str, frontend: str) -> None:
    inst = _active(reg, name)
    if frontend == "webui":   # first: until the switch is saved, config apply keeps the Webapp
        install_webui(name)
    inst.frontend = frontend
    if password := ensure_sign_in(inst):
        print(f"{name}: new password: {password}   (for both frontends; shown once)")
    reg.save()   # then config apply and a repeat finish the switch
    configure_webapp(s, inst)
    start_or_restart_gateway(s, inst)
    print(f"{name}: {FRONTEND_NAMES[frontend]} at {inst.url}")


def limits(reg: Registry, name: str, cpu: int | None, memory_mb: int | None, disk_gb: int | None) -> None:
    _active(reg, name)
    if not (cpu or memory_mb or disk_gb):
        raise CommandError("give --cpu, --memory-mb or --disk-gb")
    set_limits(name, cpu, memory_mb, disk_gb)


def provider_of(s: Settings, reg: Registry, name: str, timeout: float = 600) -> str:
    """The provider the instance uses: its lock's, or the one in its own config, printable, as its users set it."""
    s = settings_for(s, _active(reg, name))
    if s.model_lock:
        return s.model_provider
    return printable(hermes(name, ["config", "get", "model.provider"], check=False, timeout=timeout).stdout.strip()) \
        or s.model_provider


def model_of(s: Settings, reg: Registry, name: str) -> tuple[str, str]:
    """The provider and model the instance uses: its lock's, or those in its own config (model: "" without one),
    printable, as its users set its config."""
    policy = settings_for(s, _active(reg, name))
    if policy.model_lock:
        return policy.model_provider, policy.model
    provider = provider_of(s, reg, name)
    return provider, printable(hermes(name, ["config", "get", "model.default"], check=False).stdout.strip())


def lock(s: Settings, reg: Registry, name: str, models: list[str]) -> None:
    """Lock the instance to these models (or the one it uses) of the provider it uses, whatever the server's lock."""
    inst = _active(reg, name)
    provider, model = model_of(s, reg, name)
    if not (models or model):
        raise CommandError(f"{name} has no model of its own: name the models: thinny instance lock {name} <model> ...")
    inst.lock, inst.lock_provider = models or [model], provider
    reg.save()  # first: its config loses its model on the way, so a failure leaves the lock for apply to finish
    update_policy(s, inst)
    restart_all(s, [], reg)
    print(f"{name} is locked to {provider}: {', '.join(inst.lock)}")


def unlock(s: Settings, reg: Registry, name: str) -> None:
    """Let the instance's users choose their models, whatever the server's lock. It starts with the model it was
    locked to (see update_policy)."""
    inst = _active(reg, name)
    inst.lock, inst.lock_provider = [], ""
    reg.save()  # first: a failure on the way leaves the unlock for apply to finish
    update_policy(s, inst)
    restart_all(s, [], reg)
    print(f"{name} is unlocked: its users choose their models")


def default_model(s: Settings, reg: Registry, name: str, model: str | None = None, provider: str | None = None) -> None:
    """Show the model the instance's new chats start on and the models it can use, or make it `model` of `provider`
    (default: the one it uses), one Hermes offers it, in its own config. Its users can still choose others; with a
    lock, the lock decides. No restart: Hermes reads it for each new chat."""
    inst = _active(reg, name)
    policy = settings_for(s, inst)
    if policy.model_lock:
        if model:
            raise CommandError(f"{name} is locked, and its lock decides its default: "
                               f"thinny instance lock {name} {model} ...")
        print(f"{name}: new chats start on {policy.model} ({_provider_name(policy.model_provider)}); "
              f"its lock allows {', '.join(policy.allowed_models)}")
        return
    # Before either is set: Hermes would refuse the second after setting the first.
    if model and (pinned := [key for key in ("model.provider", "model.default") if key in pinned_keys(policy)]):
        raise CommandError(f"{name}'s group {inst.group} decides its {' and '.join(pinned)}: "
                           f"thinny config edit --group {inst.group}")
    uses, default = model_of(s, reg, name)
    available = available_models(name)
    if not model:
        print(f"{name}: new chats start on {default} ({_provider_name(uses)})" if default else
              f"{name}: no model is set for new chats; pick one: thinny instance model {name} <model>")
        providers = dict.fromkeys([*available, uses, *PROVIDER_NAMES])   # and thinny's, signed in or not
        width = max(len(_provider_name(p)) for p in providers)
        for p in providers:
            text = ", ".join(available[p]) if p in available else f"not signed in (thinny instance login {name})"
            print(textwrap.fill(f"{_provider_name(p):<{width}}  {text}", 100, subsequent_indent=" " * (width + 2),
                                break_long_words=False, break_on_hyphens=False))
        return
    provider = provider or uses
    if provider not in available:
        raise CommandError(f"{name} is not signed in to {_provider_name(provider)}: thinny instance login {name}")
    # Hermes lists Claude's long-context models with [1m], and takes them without it too, as thinny suggests them.
    if model not in available[provider] and f"{model}[1m]" not in available[provider]:
        raise CommandError(f"{_provider_name(provider)} offers {name} no model {model}; "
                           f"it offers {', '.join(available[provider])}")
    for key, value in (("model.provider", provider), ("model.default", model)):
        hermes(name, ["config", "set", key, value])
    print(f"{name}: new chats start on {model} ({_provider_name(provider)})")


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


def password(s: Settings, reg: Registry, name: str, ask: Callable[[str], str] | None = None) -> None:
    """A new password for the instance's frontend: a random one, or the one `ask` gets once it is known to be
    active."""
    inst = _active(reg, name)
    typed = ask(name) if ask else None
    if inst.frontend == "webui":
        mark_restart([name])   # first: a failure leaves it for the next restart
        as_user(name, ["systemctl", "--user", "stop", "hermes-webui"], timeout=RESTART_TIMEOUT)
    new = reset_password(name, typed)
    start_or_restart_gateway(s, inst)
    if typed:
        print(f"{name}: new {FRONTEND_NAMES[inst.frontend]} password set   (open browsers must sign in again)")
        return
    print(f"{name}: new {FRONTEND_NAMES[inst.frontend]} password: {new}   (shown once; open browsers must sign in "
          "again)")


def disable(s: Settings, reg: Registry, name: str) -> None:
    """Disable an instance: its container stops for good; its data stays in it."""
    inst = reg.get(name)
    if inst.state == "disabled":
        print(f"{name} is already disabled")
        return
    if inst.state == "removed":
        raise RegistryError(f"{name} is removed")
    if container_state(name) is not None:
        stop(name)
    inst.state = "disabled"
    reg.save()
    host.reconcile_access(s, reg)
    print(f"{name} disabled: container stopped, Webapp and agent with it. Its data stays in the container {name}.")


def remove(s: Settings, reg: Registry, name: str, purge: bool = False, keep_mailbox: bool = False) -> None:
    """Once the admin types its name, delete a disabled or pending instance's container, with all its data, and its
    mailbox unless `keep_mailbox` (e.g. another server's after a move). Its registry entry stays, so its name, port
    and address are not reused, and so does its access trace. Purged, a removed one too leaves the registry,
    freeing them, and its access trace stays under another name."""
    inst = reg.get(name)
    if inst.state == "active":
        raise CommandError(f"{name} is active; disable it first: thinny instance disable {name}")
    if inst.state == "removed" and not purge:
        print(f"{name} is already removed")
        return
    if not sys.stdin.isatty():
        raise CommandError("no terminal to confirm on; nothing removed")
    if inst.state != "removed":
        print(f"This deletes {name}'s container, with all its data.")
    if inst.mailbox:
        print(f"Its mailbox {inst.mailbox} stays in Purelymail." if keep_mailbox else
              f"This deletes its mailbox {inst.mailbox} too, with all its mail.")
    if purge:
        print(f"This frees {name}'s name and port for a new instance.")
    try:
        answer = input(f"Type its name ({name}) to remove it: ")
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer.strip() != name:
        raise CommandError("nothing removed")

    if inst.mailbox:   # first: when Purelymail fails, the instance stays as it is
        if not keep_mailbox:
            purelymail.delete(inst.mailbox)
        inst.mailbox = ""
        reg.save()   # at once: a mailbox created at its address later is not thinny's to delete
    if container_state(name) is not None:
        run(["incus", "delete", "--force", name])
    if (host.POLICIES / name).exists():
        shutil.rmtree(host.POLICIES / name)
    kept = trace.DIR / name
    if purge:
        if kept.exists():   # a new instance of the name starts a trace of its own
            kept = kept.rename(trace.DIR / f"{name}.purged-{time.strftime('%Y-%m-%dT%H%M%S')}")
        if (trace.UNSENT / name).exists():   # not for the sheet of a new instance of the name
            shutil.rmtree(trace.UNSENT / name)
        del reg.instances[name]
    else:
        inst.state = "removed"
    host.reconcile_access(s, reg)   # a pending instance's Webapp may be published
    reg.save()   # last: a failure leaves a remove to run again
    print(f"{name} {'purged' if purge else 'removed'}. Its access trace stays in {kept}.")


def stop(name: str) -> None:
    """Stop the instance's container and keep it stopped."""
    run(["incus", "config", "set", name, "boot.autostart=false"])  # first: a reboot must not bring it back
    if container_state(name) == "RUNNING" and \
            run(["incus", "stop", name, "--timeout", "60"], check=False, timeout=120).returncode != 0:
        run(["incus", "stop", name, "--force"])  # its root may refuse to shut down
    if container_state(name) == "RUNNING":
        raise CommandError(f"{name}'s container is still running")


def set_group(s: Settings, reg: Registry, name: str, group: str) -> None:
    """Put the instance in the group ("": in none); its policy pins the group's Hermes settings."""
    inst = _active(reg, name)
    inst.group = group
    reg.save()  # first: a failure on the way leaves the change for apply to finish
    update_policy(s, inst)
    restart_all(s, [], reg)
    print(f"{name} is in the group {group}" if group else f"{name} is in no group")


def list_groups(s: Settings, reg: Registry) -> None:
    members: dict[str, list[str]] = {}
    for inst in reg.current():
        if inst.group:
            members.setdefault(inst.group, []).append(inst.name)
    rows = [("GROUP", "INSTANCES")]
    for group in sorted(s.groups.keys() | members.keys()):
        missing = "" if group in s.groups else "   (its file is missing)"
        rows.append((group, (", ".join(members.get(group, [])) or "-") + missing))
    width = max(len(group) for group, _ in rows)
    for group, names in rows:
        print(f"{group:<{width}}  {names}")


def list_instances(reg: Registry, group: str | None = None) -> None:
    """CPU is how many of its CPUs an instance kept busy over a second; each instance's gateway, disk use
    and phone line are read meanwhile. `group`: its members only. A column with nothing in it (only -) is left out."""
    shown = [inst for inst in sorted(reg.instances.values(), key=lambda inst: inst.port)
             if group is None or inst.group == group]

    def probe(inst: Instance) -> tuple[bool | None, int, str]:
        """Whether its gateway is active (None: a disabled or removed instance shows none), its disk use, and
        whether it has a phone line (as add or the Webapp sets one up)."""
        try:
            phone = "yes" if env_values(inst.name).get("SIP_USER") else "-"
        except CommandError:   # its users' .env, too large or not text
            phone = "?"
        return (None if inst.state in ("disabled", "removed") else gateway_active(inst.name), disk_used(inst.name),
                phone)

    before = _containers()
    start = time.monotonic()
    with ThreadPoolExecutor() as pool:
        probes = {inst.name: pool.submit(probe, inst) for inst in shown if inst.name in before}
        time.sleep(1)
        containers = _containers()
        seconds = time.monotonic() - start
    rows = [("NAME", "STATE", "GROUP", "FRONTEND", "GATEWAY", "CPU", "RAM", "DISK", "MAIL", "PHONE", "URL")]
    for inst in shown:
        c = containers.get(inst.name)
        gateway = cpu = ram = disk = phone = "-"
        if c:
            # A container started during the second is read now.
            active, used, phone = probes[inst.name].result() if inst.name in probes else probe(inst)
            if active is not None:
                gateway = "active" if active else "INACTIVE"
            state = c["state"]
            disk = _gib(used, state["disk"]["root"]["total"])
            if c["status"] == "Running":
                ram = _gib(_memory_used(inst.name, state["memory"]["usage"]), state["memory"]["total"])
                if before.get(inst.name, {}).get("status") == "Running":   # not started in between
                    used = state["cpu"]["usage"] - before[inst.name]["state"]["cpu"]["usage"]   # in ns
                    cpu = f"{used / 1e9 / seconds:.1f}/{c['expanded_config']['limits.cpu']}"
        rows.append((inst.name, inst.state, inst.group or "-", inst.frontend, gateway, cpu, ram, disk,
                     "yes" if inst.mailbox else "-", phone, inst.url.rstrip("/")))
    columns = [column for column in zip(*rows) if set(column[1:]) != {"-"}]
    widths = [max(map(len, column)) for column in columns]
    for r in zip(*columns):
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def usage(s: Settings, reg: Registry, name: str | None = None) -> None:
    """How much of their subscription's limits the users of every active instance (or the named one) used, as
    Hermes asks the provider now. All at once: one after another, they would add up to minutes."""
    targets = [_active(reg, name)] if name else [inst for inst in reg.current() if inst.state == "active"]
    with ThreadPoolExecutor() as pool:
        rows = [("NAME", "PROVIDER", "ACCOUNT", "5H", "WEEK"),
                *pool.map(lambda inst: _usage_row(s, reg, inst.name), targets)]
    # With its users' texts (a provider, an account, a reason): without control characters, for the admin's terminal.
    rows = [[printable(c) for c in r] for r in rows]
    # A row's last text sets no column's width: a note (not signed in, ?) spans 5H and WEEK.
    widths = [max(len(r[i]) for r in rows if len(r) > i + 1) for i in range(4)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, [*widths, 0])).rstrip())


def _usage_row(s: Settings, reg: Registry, name: str) -> tuple[str, ...]:
    shown = account = "?"
    try:
        provider = provider_of(s, reg, name, timeout=USAGE_TIMEOUT)
        shown, account = _provider_name(provider), _account(name, provider)
        res = hermes(name, ["usage", "--json", "--provider", provider], check=False, timeout=USAGE_TIMEOUT)
        if res.returncode:   # Hermes got nothing: without a credential, or the provider did not answer
            status = hermes(name, ["auth", "status", provider], check=False, timeout=USAGE_TIMEOUT).stdout
            return name, shown, account, "not signed in" if ": logged out" in status else "?"
        doc = json.loads(res.stdout)
        if reason := doc["unavailable_reason"]:   # the Claude plugin's way of saying it got nothing
            return (name, shown, account,
                    "not signed in" if reason.startswith("no Claude subscription login") else reason)
        windows = {w["label"]: w for w in doc["windows"]}
        return name, shown, account, _window(windows, FIVE_HOURS, "%H:%M"), _window(windows, WEEK, "%a %H:%M")
    except (CommandError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
            RecursionError):   # or what its users made Hermes say
        return name, shown, account, "?"


def _account(name: str, provider: str) -> str:
    """The email of the account the instance is signed in to the provider with; - when there is none, ? when it
    is unknown. Claude Code tells it; Hermes keeps ChatGPT's in its tokens, but does not show it. Hermes may hold
    several, and picks one per request: each of their emails."""
    try:
        if provider == CLAUDE_PROVIDER:
            status = as_user(name, ["claude", "auth", "status"], check=False, timeout=USAGE_TIMEOUT).stdout
            return str(json.loads(status).get("email") or "-")
        if provider == "openai-codex":
            auth = json.loads(read_user_file(name, str(home(name) / "auth.json")) or "{}")
            emails = []
            for credential in auth.get("credential_pool", {}).get(provider, []):
                payload = credential["access_token"].split(".")[1]   # of a JWT
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                emails.append(str(claims["https://api.openai.com/profile"]["email"]))
            return ", ".join(dict.fromkeys(emails)) or "-"
        return "-"
    except (CommandError, ValueError, TypeError, KeyError, IndexError, AttributeError,
            RecursionError):   # or what its users wrote
        return "?"


def _window(windows: dict[str, dict], labels: tuple[str, ...], form: str) -> str:
    """The share of a limit used, and when it resets in the server's time (`form`), as `37% → 21:00`."""
    window = next((windows[label] for label in labels if label in windows), None)
    if not window or window["used_percent"] is None:
        return "-"
    reset = window["resets_at"] and dt.datetime.fromisoformat(window["resets_at"]).astimezone()
    return f"{window['used_percent']:.0f}%" + (f" → {reset:{form}}" if reset else "")


def _provider_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider)


def _containers() -> dict[str, dict]:
    """Every container as Incus describes it, with its state: name -> container."""
    return {c["name"]: c for c in json.loads(run(["incus", "list", "--format", "json"]).stdout)}


def _memory_used(name: str, usage: int) -> int:
    """Bytes the container uses without its file cache, which the kernel frees when it needs the memory. Incus's
    `usage` counts the cache, so an idle instance looked near its limit; it is the answer without the cgroup."""
    cgroup = CGROUPS / f"lxc.payload.{name}"
    try:
        stat = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
        return int((cgroup / "memory.current").read_text()) - int(stat["active_file"]) - int(stat["inactive_file"])
    except OSError:
        return usage


def _gib(used: int, total: int) -> str:
    """Used and total GiB, as `0.7/4G`."""
    total_gib = f"{total / 2**30:.1f}".removesuffix(".0")
    return f"{used / 2**30:.1f}/{total_gib}G"


def _active(reg: Registry, name: str) -> Instance:
    inst = reg.get(name)
    if inst.state != "active":
        raise RegistryError(f"{name} is {inst.state}")
    return inst
