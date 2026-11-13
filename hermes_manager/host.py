"""The host-wide pieces: Incus storage and profile, firewall, units, the managed scope, the
Hermes release everyone runs, and the proxies that publish Webapps."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import urllib.request
from pathlib import Path

from . import render
from .registry import Registry
from .settings import Settings
from .system import STATE_DIR, log, run, tailscale_status, write_file

MANAGED = Path("/etc/hermes")
LIB = Path(__file__).resolve().parent.parent      # this repository, installed to /usr/local/lib/hermes-manager
FILES = LIB / "files"
CADDYFILE = Path("/etc/caddy/Caddyfile")
FIREWALL_FILE = Path(render.FIREWALL_FILE)
SYSCTL_FILE = Path("/etc/sysctl.d/90-hermes-manager.conf")
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


def release() -> tuple[str, str] | None:
    """(tag, commit) of the Hermes release everyone runs and new people get; None before the first
    person."""
    if not RELEASE_FILE.exists():
        return None
    tag, commit = RELEASE_FILE.read_text().split()
    return tag, commit


def first_release() -> tuple[str, str]:
    """The latest stable release becomes the one everyone runs."""
    tag, _ = latest_release()
    write_release(tag, commit_of(tag))
    return release()


def write_release(tag: str, commit: str) -> None:
    write_file(RELEASE_FILE, f"{tag} {commit}\n")


def installer() -> str:
    """The official Hermes installer."""
    with urllib.request.urlopen(INSTALLER_URL, timeout=60) as r:
        return r.read().decode()


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


def init_incus() -> None:
    """Incus storage: a btrfs pool in a sparse file (instant copies and snapshots, a disk quota per
    container), sized once to most of the free space. No Incus network: containers get routed NICs."""
    if run(["incus", "storage", "show", render.POOL], check=False).returncode == 0:
        return
    size = int(shutil.disk_usage("/var/lib").free * 0.8) // 2**30
    log(f"creating the Incus storage pool ({size} GiB at most; it takes space as people use it)")
    run(["incus", "storage", "create", render.POOL, "btrfs", f"size={size}GiB"], timeout=600)


def ensure_profile(s: Settings) -> None:
    """The profile of every person container; changed limits apply to running containers too."""
    if run(["incus", "profile", "show", render.PROFILE], check=False).returncode != 0:
        run(["incus", "profile", "create", render.PROFILE])
    run(["incus", "profile", "edit", render.PROFILE], input=render.profile(s))


def install_units(s: Settings) -> None:
    changed = [
        write_file(SYSTEMD / "incus.service.d/hermes-manager.conf", render.INCUS_DROPIN),
        write_file(SYSTEMD / "hermes-manager-update.service", render.UPDATE_SERVICE),
        write_file(SYSTEMD / "hermes-manager-update.timer", render.update_timer(s)),
    ]
    if any(changed):
        run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "hermes-manager-update.timer"])
    write_file(SYSCTL_FILE, render.SYSCTL)
    run(["/usr/lib/systemd/systemd-sysctl", str(SYSCTL_FILE)])  # as at boot; sysctl -p ignores "-"


def firewall(s: Settings) -> None:
    """Load the containers' firewall table (incus.service loads the same file before it starts).
    Never restart nftables.service: on Debian that flushes every table, Tailscale's included."""
    write_file(FIREWALL_FILE, render.firewall(s.network_interface, list(s.blocked_networks)))
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
