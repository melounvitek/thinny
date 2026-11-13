"""The shared install and the host-wide pieces: Hermes in /opt/hermes, plugins, managed scope,
firewall, units and the proxies that publish Webapps."""

from __future__ import annotations

import datetime as dt
import fcntl
import io
import json
import re
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import render
from .registry import Registry
from .settings import Settings
from .system import (ROOT_HERMES_HOME_DIR, STATE_DIR, CommandError, hermes_root, log, run, slice_uids,
                     tailscale_status, uid_of, write_file)

INSTALL = Path(render.INSTALL)
CHECKOUT = Path(render.CHECKOUT)
MANAGED = Path("/etc/hermes")
LIB = Path(__file__).resolve().parent.parent      # this repository, installed to /usr/local/lib/hermes-manager
FILES = LIB / "files"
WEBAPP_INDEX = CHECKOUT / "apps/desktop/dist-webapp/index.html"
CADDYFILE = Path("/etc/caddy/Caddyfile")
FIREWALL_FILE = Path("/etc/hermes-manager/hermes-manager.nft")
FIREWALL_LOCK = Path("/run/hermes-manager-firewall.lock")
SYSTEMD = Path("/etc/systemd/system")
REPO_API = "https://api.github.com/repos/NousResearch/hermes-agent"
INSTALLER_URL = "https://hermes-agent.nousresearch.com/install.sh"
RELEASE_FILE = STATE_DIR / "release"   # "<tag> <commit>": the Hermes release everyone runs


def _github(path: str) -> dict:
    req = urllib.request.Request(REPO_API + path, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "hermes-manager"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def latest_release() -> tuple[str, dt.datetime]:
    """Tag and publication time of the latest stable Hermes release."""
    data = _github("/releases/latest")
    return data["tag_name"], dt.datetime.fromisoformat(data["published_at"].replace("Z", "+00:00"))


def commit_of(tag: str) -> str:
    return _github(f"/commits/{tag}")["sha"]


def release() -> tuple[str, str]:
    """(tag, commit) of the Hermes release everyone runs and new people get. The updates record it;
    before the first person it becomes the latest stable release."""
    if not RELEASE_FILE.exists():
        tag, _ = latest_release()
        write_release(tag, commit_of(tag))
    tag, commit = RELEASE_FILE.read_text().split()
    return tag, commit


def write_release(tag: str, commit: str) -> None:
    write_file(RELEASE_FILE, f"{tag} {commit}\n")


def installer() -> str:
    """The official Hermes installer."""
    with urllib.request.urlopen(INSTALLER_URL, timeout=60) as r:
        return r.read().decode()


def installed_tags() -> set[str]:
    return set(run(["git", "-C", str(CHECKOUT), "tag", "--points-at", "HEAD"], check=False).stdout.split())


def install_hermes() -> None:
    """The official installer into /opt/hermes, only when there is no install yet: rerunning it
    would update Hermes outside the careful nightly update."""
    if (CHECKOUT / ".git").exists():
        return
    log(f"installing the latest stable Hermes into {INSTALL} (takes a few minutes)")
    with urllib.request.urlopen(INSTALLER_URL, timeout=60) as r:
        script = r.read()
    ROOT_HERMES_HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    installer = ROOT_HERMES_HOME_DIR / "hermes-install.sh"
    installer.write_bytes(script)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
           "HOME": str(ROOT_HERMES_HOME_DIR), "HERMES_HOME": str(INSTALL), "HERMES_HOME_MODE": "0711"}
    run(["bash", str(installer), "--channel", "stable", "--non-interactive"], env=env,
        cwd=ROOT_HERMES_HOME_DIR, timeout=3600)


def fix_permissions() -> None:
    """People borrow the install read-only; root's Hermes runs and updates tighten it."""
    INSTALL.chmod(0o711)
    for sub in ("hermes-agent", "tools", "installs"):
        if (INSTALL / sub).is_dir():
            run(["chmod", "-R", "a+rX", str(INSTALL / sub)])


def install_files() -> None:
    write_file(Path("/usr/local/bin/hermes"), render.launcher(), 0o755)
    write_file(Path("/usr/local/bin/hermes-podman"), (FILES / "hermes-podman").read_text(), 0o755)


def ensure_plugins(s: Settings) -> None:
    """model-guard and the language pack in the install's bundled plugins, hidden from git."""
    shutil.copytree(FILES / "model-guard", CHECKOUT / "plugins" / "model-guard", dirs_exist_ok=True)
    names = ["model-guard"]
    if s.language_pack:
        _ensure_catalog_plugin(s.language_pack)
        names.append(s.language_pack)
    exclude = CHECKOUT / ".git/info/exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [f"/plugins/{n}/" for n in names if f"/plugins/{n}/" not in lines]
    if missing:
        exclude.write_text("\n".join(lines + missing) + "\n")


def _ensure_catalog_plugin(name: str) -> None:
    """A catalog plugin at the commit the installed Hermes catalog pins."""
    entry = CHECKOUT / "plugin-catalog" / f"{name}.yaml"
    if not entry.exists():
        raise CommandError(f"language pack {name!r} is not in the Hermes plugin catalog")
    text = entry.read_text()
    repo = re.search(r"^repo:\s*(\S+)", text, re.M).group(1)
    sha = re.search(r"^sha:\s*([0-9a-f]{40})", text, re.M).group(1)
    target = CHECKOUT / "plugins" / name
    marker = target / ".catalog-sha"
    if marker.exists() and marker.read_text().strip() == sha:
        return
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(\.git)?/?", repo)
    if not match:
        raise CommandError(f"language pack {name!r}: unsupported repository {repo}")
    log(f"installing plugin {name} at {sha[:10]}")
    with urllib.request.urlopen(f"https://codeload.github.com/{match.group(1)}/tar.gz/{sha}", timeout=120) as r:
        data = r.read()
    with tempfile.TemporaryDirectory(dir=CHECKOUT / "plugins") as tmp, \
            tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(tmp, filter="data")
        (top,) = Path(tmp).iterdir()
        (top / ".catalog-sha").write_text(sha + "\n")
        shutil.rmtree(target, ignore_errors=True)
        top.rename(target)


def build_webapp(force: bool = False) -> None:
    """The official installer does not build the Webapp, and people cannot build into the
    root-owned checkout."""
    if force or not WEBAPP_INDEX.exists():
        log("building the Hermes Webapp (takes a few minutes)")
        hermes_root(["webapp", "--build-only"], timeout=3600)


def managed_files(s: Settings) -> dict[Path, str]:
    return {MANAGED / "config.yaml": render.managed_config(s),
            MANAGED / ".env": render.managed_env(),
            MANAGED / "allowed-models.txt": render.allowed_models(s),
            MANAGED / "model-guard-message.txt": render.model_guard_message(s)}


def managed_scope_current(s: Settings) -> bool:
    return all(path.is_file() and path.read_text(encoding="utf-8") == text for path, text in managed_files(s).items())


def write_managed_scope(s: Settings) -> None:
    for path, text in managed_files(s).items():
        write_file(path, text)


def install_units(s: Settings) -> None:
    changed = [
        write_file(SYSTEMD / "hermes-manager-firewall.service", render.FIREWALL_UNIT),
        write_file(SYSTEMD / "user@.service.d/hermes-manager.conf", render.USER_MANAGER_DROPIN),
        write_file(SYSTEMD / "hermes-manager-update.service", render.UPDATE_SERVICE),
        write_file(SYSTEMD / "hermes-manager-update.timer", render.update_timer(s)),
    ]
    if any(changed):
        run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "hermes-manager-firewall.service", "hermes-manager-update.timer"])


def proxy_uids(s: Settings, reg: Registry) -> list[int]:
    """Accounts allowed to connect to people's Webapps besides root; never a person."""
    people = {p.uid for p in reg.people.values()}
    return sorted(uid for uid in map(uid_of, [*s.proxy_users, "caddy"]) if uid is not None and uid not in people)


def firewall(s: Settings) -> None:
    """Load the person-isolation table from the registry and the user slices as they are now.
    Several user managers start at once at boot, so loads are serialised: a load with an older
    snapshot must not replace a newer one. Never restart nftables.service: on Debian that
    flushes every table, Tailscale's included."""
    with FIREWALL_LOCK.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        reg = Registry.load()
        text = render.firewall(people=[(p.uid, p.port) for p in reg.people.values() if p.uid is not None],
                               slice_uids=slice_uids(), proxy_uids=proxy_uids(s, reg),
                               blocked_networks=list(s.blocked_networks),
                               # installed, not running: at boot the table loads before tailscaled is up
                               tailscale=shutil.which("tailscale") is not None)
        write_file(FIREWALL_FILE, text, 0o644)
        run(["nft", "-c", "-f", str(FIREWALL_FILE)])
        run(["nft", "-f", str(FIREWALL_FILE)])


def reconcile_access(s: Settings, reg: Registry) -> None:
    """Caddy sites for LAN people, `tailscale serve` for Tailscale people; nothing else
    published in the Webapp port range."""
    lan = [p.port for p in reg.current() if p.access == "lan"]
    if lan or CADDYFILE.exists() and "hermes-manager" in CADDYFILE.read_text():
        if lan and not shutil.which("caddy"):
            log("installing Caddy for office-network access")
            run(["apt-get", "install", "-y", "-q", "caddy"], env={"DEBIAN_FRONTEND": "noninteractive",
                                                                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, timeout=900)
            firewall(s)  # the new caddy account may now reach people's Webapps
        if write_file(CADDYFILE, render.caddyfile(s, lan)):
            run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"], check=False)
            run(["systemctl", "restart", "caddy"])  # no admin API, so no reload

    status = tailscale_status()
    if status is None:
        return
    wanted = {p.port for p in reg.current() if p.access == "tailscale"}
    served = _tailscale_served_ports()
    low, high = s.port_range
    for port in sorted(wanted - served):
        run(["tailscale", "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}"])
    for port in sorted(served - wanted):
        if low <= port <= high:
            run(["tailscale", "serve", f"--https={port}", "off"])


def _tailscale_served_ports() -> set[int]:
    res = run(["tailscale", "serve", "status", "--json"], check=False)
    try:
        tcp = json.loads(res.stdout or "{}").get("TCP") or {}
    except json.JSONDecodeError:
        return set()
    return {int(port) for port in tcp}


def caddy_root_ca() -> Path:
    return Path("/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt")
