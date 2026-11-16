"""The host-wide pieces: Incus storage and profile, firewall, units, the managed scope, the
Hermes release all instances run, and the proxies that publish Webapps."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import shutil
import urllib.request
from pathlib import Path

from . import render
from .registry import Registry
from .settings import Settings
from .system import STATE_DIR, CommandError, log, run, tailscale_status, write_file

MANAGED = Path("/etc/hermes")   # Hermes's managed scope in each container (on the host, the one they once shared)
POLICIES = STATE_DIR / "policies"   # each instance's managed scope, in a folder of its own
LIB = Path(__file__).resolve().parent.parent      # this repository, installed to /usr/local/lib/thinny
THINNY = "/usr/local/sbin/thinny"
FILES = LIB / "files"
CADDYFILE = Path("/etc/caddy/Caddyfile")
OFFICE_CA = Path("/etc/thinny/office-root.crt")   # Caddy's root certificate, readable by any account
FIREWALL_FILE = Path(render.FIREWALL_FILE)
SYSCTL_FILE = Path("/etc/sysctl.d/90-thinny.conf")
UFW_CONF = Path("/etc/ufw/ufw.conf")
UFW_FORWARD = "ufw route allow in on thinny-+"   # lets the containers' traffic through ufw (README, Prerequisites)
SYSTEMD = Path("/etc/systemd/system")
HERMES_REPO = "NousResearch/hermes-agent"
WEBUI_REPO = "nesquena/hermes-webui"
INSTALLER_URL = "https://hermes-agent.nousresearch.com/install.sh"
CATALOG_URL = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/plugin-catalog/{}.yaml"
RELEASE_FILE = STATE_DIR / "release"   # "<tag> <commit>": the Hermes release all instances run
WEBUI_RELEASE_FILE = STATE_DIR / "webui-release"   # the same for WebUI, of the instances that use it
DASHBOARD_STATE = STATE_DIR / "dashboard"   # "<port> <access>" while thinny runs the dashboard (dashboard.py)


def _github(repo: str, path: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "thinny"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def latest_release(repo: str = HERMES_REPO) -> tuple[str, dt.datetime]:
    """Tag and publication time of the latest stable release (of Hermes, or `repo`)."""
    data = _github(repo, "/releases/latest")
    return data["tag_name"], dt.datetime.fromisoformat(data["published_at"].replace("Z", "+00:00"))


def commit_of(tag: str, repo: str = HERMES_REPO) -> str:
    return _github(repo, f"/commits/{tag}")["sha"]


def release(path: Path | None = None) -> tuple[str, str] | None:
    """(tag, commit) of the Hermes release (or WebUI's, in WEBUI_RELEASE_FILE) all instances run and new ones get;
    None before the first instance."""
    path = path or RELEASE_FILE
    if not path.exists():
        return None
    tag, commit = path.read_text().split()
    return tag, commit


def first_release(repo: str = HERMES_REPO, path: Path | None = None) -> tuple[str, str]:
    """The latest stable release becomes the one all instances run."""
    tag, _ = latest_release(repo)
    write_release(tag, commit_of(tag, repo), path)
    return release(path)


def write_release(tag: str, commit: str, path: Path | None = None) -> None:
    write_file(path or RELEASE_FILE, f"{tag} {commit}\n")


def installer() -> str:
    """The official Hermes installer."""
    with urllib.request.urlopen(INSTALLER_URL, timeout=60) as r:
        return r.read().decode()


def policy_files(s: Settings, name: str) -> dict[Path, str]:
    return {POLICIES / name / "config.yaml": render.managed_config(s),
            POLICIES / name / ".env": render.managed_env(),
            POLICIES / name / "allowed-models.txt": render.allowed_models(s),
            POLICIES / name / "model-guard-message.txt": render.model_guard_message(s)}


def policy_current(s: Settings, name: str) -> bool:
    return all(path.is_file() and path.read_text(encoding="utf-8") == text
               for path, text in policy_files(s, name).items())


def write_policy(s: Settings, name: str) -> None:
    for path, text in policy_files(s, name).items():
        write_file(path, text)


def init_incus() -> None:
    """Incus storage: a btrfs pool in a sparse file (instant copies and snapshots, a disk quota per
    container), sized once to most of the free space. No Incus network: containers get routed NICs."""
    if run(["incus", "storage", "show", render.POOL], check=False).returncode != 0:
        size = int(shutil.disk_usage("/var/lib").free * 0.8) // 2**30
        log(f"creating the Incus storage pool ({size} GiB at most; it takes space as instances use it)")
        run(["incus", "storage", "create", render.POOL, "btrfs", f"size={size}GiB"], timeout=600)
    # Compressed, which about halves an instance's disk use (the quota counts the compressed size). Forced:
    # btrfs's own guess left most of Chromium and Claude Code uncompressed. Incus's default option first. A pool
    # from before gets it at once, remounted, for what is written from then on.
    run(["incus", "storage", "set", render.POOL,
         "btrfs.mount_options=user_subvol_rm_allowed,compress-force=zstd:1"])


def ensure_profile(s: Settings) -> None:
    """The profile of every instance container; changed limits apply to running containers too."""
    if run(["incus", "profile", "show", render.PROFILE], check=False).returncode != 0:
        run(["incus", "profile", "create", render.PROFILE])
    run(["incus", "profile", "edit", render.PROFILE], input=render.profile(s))


def install_units(s: Settings) -> None:
    changed = [
        write_file(SYSTEMD / "incus.service.d/thinny.conf", render.INCUS_DROPIN),
        write_file(SYSTEMD / "thinny-update.service", render.UPDATE_SERVICE),
        write_file(SYSTEMD / "thinny-update.timer", render.update_timer(s)),
        write_file(SYSTEMD / "thinny-trace.service", render.TRACE_SERVICE),
    ]
    if any(changed):
        run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "thinny-update.timer"])
    run(["systemctl", "enable", "thinny-trace.service"])
    run(["systemctl", "restart", "thinny-trace.service"])  # install.sh may have brought a new thinny
    write_file(SYSCTL_FILE, render.SYSCTL)
    run(["/usr/lib/systemd/systemd-sysctl", str(SYSCTL_FILE)])  # as at boot; sysctl -p ignores "-"


def firewall(s: Settings) -> None:
    """Load the containers' firewall table (incus.service loads the same file before it starts).
    Never restart nftables.service: on Debian that flushes every table, Tailscale's included."""
    write_file(FIREWALL_FILE, render.firewall(s.network_interface, list(s.blocked_networks)))
    run(["nft", "-c", "-f", str(FIREWALL_FILE)])
    run(["nft", "-f", str(FIREWALL_FILE)])


def ufw_lacks(rules: list[str]) -> list[str]:
    """Those of `rules` (ufw commands) ufw has not got while it is on; none while it is off or busy."""
    if not shutil.which("ufw") or "ENABLED=yes" not in UFW_CONF.read_text().splitlines():
        return []
    try:
        added = {line.partition(" comment ")[0]   # ufw lists a rule as its command
                 for line in run(["ufw", "show", "added"], check=False, timeout=30).stdout.splitlines()}
    except CommandError:   # ufw waits for its lock while an admin's ufw command asks to confirm
        return []
    return [rule for rule in rules if rule not in added]


def ufw_lacks_forward() -> bool:
    """ufw is on without UFW_FORWARD, so the containers cannot reach the internet."""
    return bool(ufw_lacks([UFW_FORWARD]))


def ufw_lan_rules(s: Settings) -> list[str]:
    """The ufw rules that let office PCs, in lan_address's network, open the Webapps, the dashboard and the root
    certificate."""
    # "2: eth0    inet 192.168.1.191/24 brd ...", nothing when no interface has lan_address
    words = run(["ip", "-o", "addr", "show", "to", s.lan_address], check=False, timeout=10).stdout.split()
    if len(words) < 4:
        return []
    office = ipaddress.ip_interface(words[3]).network
    if office.num_addresses == 1:   # a /32 or /128, or a peer address: no office network around it
        return []
    low, high = s.port_range
    dashboard_port, dashboard = dashboard_access() or (0, "")
    return [f"ufw allow from {office} to any port {low}:{high} proto tcp",
            *([f"ufw allow from {office} to any port {dashboard_port} proto tcp"] if dashboard == "lan" else []),
            f"ufw allow from {office} to any port 80 proto tcp"]


def reconcile_access(s: Settings, reg: Registry) -> None:
    """Caddy sites for LAN instances and, with a tunnel port, url instances; `tailscale serve` for
    Tailscale instances; nothing else published in the Webapp port range. The dashboard's port too, like an
    instance's (dashboard.py stops serving an old one over Tailscale)."""
    dashboard_port, dashboard = dashboard_access() or (0, "")
    lan = [inst.port for inst in reg.current() if inst.access == "lan"]
    if dashboard == "lan":
        lan.append(dashboard_port)
    tunnel = [(inst.url, inst.port) for inst in reg.current() if inst.access == "url"] if s.tunnel_port else []
    if lan or tunnel or CADDYFILE.exists() and "thinny" in CADDYFILE.read_text():
        if (lan or tunnel) and not shutil.which("caddy"):
            log("installing Caddy")
            run(["apt-get", "install", "-y", "-q", "caddy"], env={"DEBIAN_FRONTEND": "noninteractive",
                                                                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, timeout=900)
        if write_file(CADDYFILE, render.caddyfile(s, lan, tunnel)):
            run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"], check=False)
            run(["systemctl", "restart", "caddy"])  # no admin API, so no reload
    # Only caddy can read its own copy. A certificate authority Caddy has not created yet is copied
    # by a later run; doctor warns until then.
    if lan and caddy_root_ca().exists():
        write_file(OFFICE_CA, caddy_root_ca().read_text())

    status = tailscale_status()
    if status is None:
        return
    wanted = {inst.port for inst in reg.current() if inst.access == "tailscale"}
    if dashboard == "tailscale":
        wanted.add(dashboard_port)
    served = tailscale_served_ports()
    low, high = s.port_range
    for port in sorted(wanted - served):
        run(["tailscale", "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}"])
    for port in sorted(served - wanted):
        if low <= port <= high:
            run(["tailscale", "serve", f"--https={port}", "off"])


def tailscale_served_ports() -> set[int]:
    """The ports `tailscale serve` publishes; an error rather than a guess."""
    out = run(["tailscale", "serve", "status", "--json"]).stdout
    try:
        return {int(port) for port in json.loads(out or "{}").get("TCP") or {}}
    except ValueError as e:
        raise CommandError(f"tailscale serve status --json: {e}") from None


def dashboard_access() -> tuple[int, str] | None:
    """Grafana's port and how users open the dashboard while thinny runs it; None while it is off."""
    if not DASHBOARD_STATE.exists():
        return None
    port, access = DASHBOARD_STATE.read_text().split()
    return int(port), access


def caddy_root_ca() -> Path:
    return Path("/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt")
