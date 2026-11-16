"""The optional dashboard (README, Dashboard): Prometheus keeps the history of Incus's metrics, of the host's CPU,
memory and filesystems, and of each instance's disk use from thinny; Grafana shows it. All of them on 127.0.0.1:
host.reconcile_access publishes Grafana's port like a Webapp's."""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import shutil
import ssl
import sys
import urllib.request
from pathlib import Path

from . import host, instances, render
from .registry import Registry
from .settings import CONFIG_TOML, MONITORING_PORTS, Settings
from .system import CommandError, log, run, step, tailscale_name, write_file

PACKAGES = ("prometheus", "prometheus-node-exporter", "grafana", "openssl")
# What each package downloads and takes on disk, in MB, with what it needs (Grafana 13.3, Prometheus 2.53).
SIZES = {"prometheus": (40, 190), "prometheus-node-exporter": (5, 16), "grafana": (400, 2000), "openssl": (2, 3)}
HISTORY_MB_PER_DAY = 10   # Prometheus's history of a server with about 20 instances (estimate)
RAM = "about 0.5 GB"      # Grafana 0.4 GB, Prometheus and its node exporter 0.1 GB (measured)
SERVICES = ("prometheus.service", "prometheus-node-exporter.service", "grafana-server.service")
METRICS_ADDRESS = "127.0.0.1:8444"
PROMETHEUS_CONFIG = CONFIG_TOML.parent / "prometheus.yml"
CERT = CONFIG_TOML.parent / "metrics.crt"   # Prometheus's certificate for Incus's metrics, which it only lets read them
KEY = CONFIG_TOML.parent / "metrics.key"
PASSWORD = CONFIG_TOML.parent / "dashboard.env"   # Grafana's first admin password
GRAFANA_KEY_URL = "https://apt.grafana.com/gpg-full.key"
GRAFANA_KEY = Path("/etc/apt/keyrings/thinny-grafana.asc")
GRAFANA_SOURCE = Path("/etc/apt/sources.list.d/thinny-grafana.list")
GRAFANA_UPGRADES = Path("/etc/apt/apt.conf.d/52thinny-grafana")
PROVISIONING = Path("/etc/grafana/provisioning")
DISK_METRICS = Path("/var/lib/prometheus/node-exporter/thinny.prom")   # read by the node exporter
APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_SUSPEND": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def apply(s: Settings, reg: Registry) -> None:
    """Turn the dashboard on or off as config.toml says. Its packages are installed only once the admin agrees, on a
    terminal; until then nothing changes."""
    if not s.dashboard_enabled:
        turn_off()
        return
    taken = [inst.name for inst in reg.current() if inst.port in (s.dashboard_port, *MONITORING_PORTS)]
    if taken:
        raise CommandError(f"the dashboard needs ports {s.dashboard_port} and {', '.join(map(str, MONITORING_PORTS))} "
                           f"on 127.0.0.1, but {', '.join(taken)} uses one: change dashboard.port, or turn it off")
    address = run(["incus", "config", "get", "core.metrics_address"]).stdout.strip()
    if address not in ("", METRICS_ADDRESS):
        raise CommandError(f"Incus serves its metrics at {address}, but the dashboard needs {METRICS_ADDRESS}: "
                           "unset core.metrics_address, or turn the dashboard off")
    url = address_of(s)   # fails while Tailscale is not running, before anything changes
    out = run(["dpkg-query", "-W", "-f=${Package} ${Status}\n", *PACKAGES], check=False).stdout
    installed = {line.split()[0] for line in out.splitlines() if line.endswith(" install ok installed")}
    missing = [package for package in PACKAGES if package not in installed]
    if missing and not confirmed(s, missing):
        return
    published = host.dashboard_access()
    moved = published != (s.dashboard_port, s.dashboard_access)
    if moved:
        unpublish(published)
        # First: from here on, turn_off takes back whatever this run gets done.
        write_file(host.DASHBOARD_STATE, f"{s.dashboard_port} {s.dashboard_access}\n")
    grafana_source()
    if missing:
        install(missing)
    configure(s)
    if moved:
        print(f"Dashboard: {url} (user admin; its first password: sudo cat {PASSWORD})")
        if s.dashboard_access == "local":
            print(f"  Open with: ssh -L {s.dashboard_port}:127.0.0.1:{s.dashboard_port} <you>@<server>")
        else:
            print("  Open it in a browser profile of its own: browsers send its sign-in to every Webapp at this "
                  "address (see README, Dashboard).")


def address_of(s: Settings) -> str:
    """The dashboard's address. Through the SSH tunnel it is localhost, which shares no cookies with the instances'
    <name>.localhost."""
    if s.dashboard_access == "lan":
        return f"https://{render.host_for_url(s.lan_address)}:{s.dashboard_port}/"
    if s.dashboard_access == "tailscale":
        return f"https://{tailscale_name()}:{s.dashboard_port}/"
    return f"http://localhost:{s.dashboard_port}/"


def confirmed(s: Settings, missing: list[str]) -> bool:
    """Whether the admin, told what installing the dashboard takes, wants it; never without a terminal."""
    if not sys.stdin.isatty():
        log("dashboard.enabled is true, but the dashboard is not installed: thinny installs it only once you "
            "confirm. Run on a terminal: thinny config apply")
        return False
    download = sum(SIZES[package][0] for package in missing)
    disk = sum(SIZES[package][1] for package in missing)
    history = HISTORY_MB_PER_DAY * s.dashboard_retention_days
    packages = ", ".join(f"{p} (from Grafana's repository)" if p == "grafana" else p for p in missing)
    print("The dashboard is on in config.toml, but not installed yet. Installing it:\n"
          + ("  - adds Grafana's apt repository (https://apt.grafana.com stable main) with its signing key, and lets\n"
             "    unattended upgrades update Grafana from it, new major versions included\n" if "grafana" in missing
             else "")
          + f"  - installs {packages}\n"
          f"  - downloads about {download} MB and takes about {disk / 1000:.1f} GB of disk, and up to about "
          f"{history / 1000:.1f} GB more for {s.dashboard_retention_days} days of history\n"
          f"  - uses {RAM} of RAM (estimate)\n"
          f"  - turns on Incus's metrics at {METRICS_ADDRESS}, for a certificate that can only read them\n"
          f"  Free disk space: {shutil.disk_usage('/var/lib').free / 1e9:.1f} GB")
    try:
        answer = input("Install the dashboard? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower().startswith("y"):
        return True
    print("Nothing installed or changed. To install it later: thinny config apply. Not to be asked again, set "
          "dashboard.enabled = false (thinny config edit).")
    return False


def install(packages: list[str]) -> None:
    # Masked, the packages cannot start their services on every address before configure() has set them up.
    run(["systemctl", "mask", *SERVICES])
    with step("installing the dashboard"):
        run(["apt-get", "update", "-q"], env=APT_ENV, timeout=900)
        run(["apt-get", "install", "-y", "-q", "--no-install-recommends", *packages], env=APT_ENV, timeout=3600)


def grafana_source() -> None:
    """Grafana's apt repository and its key, which unattended upgrades update Grafana from."""
    if not GRAFANA_KEY.exists():
        refresh_key()
    write_file(GRAFANA_SOURCE, render.grafana_source(str(GRAFANA_KEY)))
    write_file(GRAFANA_UPGRADES, render.GRAFANA_UPGRADES)


def refresh_key() -> None:
    """Grafana's signing key as Grafana publishes it now (it renews it before it expires)."""
    with urllib.request.urlopen(GRAFANA_KEY_URL, timeout=60) as r:
        key = r.read().decode()
    if not key.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        raise CommandError(f"{GRAFANA_KEY_URL} is not an OpenPGP key")
    write_file(GRAFANA_KEY, key)


def renew_key() -> None:
    """Grafana's key afresh while its repository is in use (thinny update): an expired one would fail every apt-get
    update, install.sh's too. A failure is only logged; the key in use stays."""
    if GRAFANA_SOURCE.exists():
        try:
            refresh_key()
        except (OSError, ValueError, CommandError) as e:   # urllib's errors are OSErrors
            log(f"Grafana's apt key not renewed: {e}")


def configure(s: Settings) -> None:
    """Incus's metrics for Prometheus's certificate, the services' settings, then the services started (those whose
    settings changed restarted) and the first file of each instance's disk use."""
    if not CERT.exists():   # made under other names, so it is there only once complete, with its key
        key, cert = KEY.with_name(f".{KEY.name}.tmp"), CERT.with_name(f".{CERT.name}.tmp")
        run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-days", "3650", "-subj", "/CN=thinny-metrics", "-keyout", str(key), "-out", str(cert)])
        key.chmod(0o600)
        run(["chown", "prometheus:prometheus", str(key)])
        key.replace(KEY)
        cert.replace(CERT)
    if not PASSWORD.exists():
        write_file(PASSWORD, f"GF_SECURITY_ADMIN_PASSWORD={secrets.token_urlsafe(18)}\n", 0o600)
    if run(["incus", "config", "get", "core.metrics_address"]).stdout.strip() != METRICS_ADDRESS:
        run(["incus", "config", "set", "core.metrics_address", METRICS_ADDRESS])
    if fingerprint() not in trusted():
        run(["incus", "config", "trust", "add-certificate", str(CERT), "--type=metrics", "--name=thinny-metrics"])
    for path, text in ((PROMETHEUS_CONFIG, render.prometheus_config(str(CERT), str(KEY))),
                       (dropin("prometheus.service"), render.prometheus_dropin(s, str(PROMETHEUS_CONFIG))),
                       (dropin("prometheus-node-exporter.service"), render.NODE_EXPORTER_DROPIN),
                       (dropin("grafana-server.service"), render.grafana_dropin(s, str(PASSWORD))),
                       (PROVISIONING / "datasources/thinny.yaml", render.GRAFANA_DATASOURCE),
                       (PROVISIONING / "dashboards/thinny.yaml", render.grafana_dashboards(str(host.FILES / "grafana"))),
                       (host.SYSTEMD / "thinny-metrics.service", render.METRICS_SERVICE),
                       (host.SYSTEMD / "thinny-metrics.timer", render.METRICS_TIMER)):
        write_file(path, text)
    run(["systemctl", "daemon-reload"])
    run(["promtool", "check", "config", str(PROMETHEUS_CONFIG)])   # Prometheus would stop on a bad one
    run(["systemctl", "unmask", *SERVICES])
    run(["systemctl", "enable", *SERVICES, "thinny-metrics.timer"])
    # Every time: a run that failed after writing their settings left them running on the old ones.
    run(["systemctl", "restart", *SERVICES, "thinny-metrics.timer"])
    write_disk_metrics()


def turn_off() -> None:
    """Take back what turning the dashboard on did: its address, its services (masked: package upgrades start no
    masked service), Incus's metrics and thinny's files for them. The packages, Grafana's password and the history
    stay. Nothing while thinny runs no dashboard."""
    published = host.dashboard_access()
    if published is None:
        return
    unpublish(published)
    if (host.SYSTEMD / "thinny-metrics.timer").exists():
        run(["systemctl", "disable", "--now", "thinny-metrics.timer"])
        run(["systemctl", "stop", "thinny-metrics.service"])
    run(["systemctl", "mask", "--now", *SERVICES])
    if CERT.exists() and (own := fingerprint()) in trusted():
        run(["incus", "config", "trust", "remove", own])
    if run(["incus", "config", "get", "core.metrics_address"]).stdout.strip() == METRICS_ADDRESS:
        run(["incus", "config", "unset", "core.metrics_address"])
    for path in (*(dropin(service) for service in SERVICES), host.SYSTEMD / "thinny-metrics.service",
                 host.SYSTEMD / "thinny-metrics.timer", PROVISIONING / "datasources/thinny.yaml",
                 PROVISIONING / "dashboards/thinny.yaml", DISK_METRICS, GRAFANA_SOURCE, GRAFANA_UPGRADES, GRAFANA_KEY):
        path.unlink(missing_ok=True)
    for service in SERVICES:
        with contextlib.suppress(OSError):   # gone, or with the admin's own drop-ins
            dropin(service).parent.rmdir()
    run(["systemctl", "daemon-reload"])
    host.DASHBOARD_STATE.unlink()


def unpublish(published: tuple[int, str] | None) -> None:
    """Stop serving the dashboard over Tailscale; Caddy's site goes with the next reconcile_access. Tailscale keeps
    what it serves while it is down, and reconcile_access leaves ports outside webapp.port_range alone, so this waits
    for Tailscale rather than forget the port."""
    if not published or published[1] != "tailscale" or not shutil.which("tailscale"):
        return
    if host.tailscale_status() is None:
        raise CommandError(f"Tailscale is not running, so thinny cannot stop serving the dashboard at port "
                           f"{published[0]}: start it, then run this again")
    if published[0] in host.tailscale_served_ports():
        run(["tailscale", "serve", f"--https={published[0]}", "off"])


def dropin(service: str) -> Path:
    return host.SYSTEMD / f"{service}.d/thinny.conf"


def fingerprint() -> str:
    """Incus's fingerprint of Prometheus's certificate."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(CERT.read_text())).hexdigest()


def trusted() -> set[str]:
    """Fingerprints of the metrics certificates Incus trusts."""
    entries = json.loads(run(["incus", "config", "trust", "list", "--format=json"]).stdout or "[]")
    return {entry["fingerprint"] for entry in entries if entry["type"] == "metrics"}


def write_disk_metrics() -> None:
    """Each instance's disk use and quota, for every instance with a container, into the node exporter's file
    (thinny metrics, every 5 minutes)."""
    containers = {c["name"]: c for c in json.loads(run(["incus", "list", "--format", "json"]).stdout)}
    rows = [(name, instances.disk_used(name), containers[name]["state"]["disk"]["root"]["total"])
            for name in sorted(Registry.load().instances) if name in containers]
    write_file(DISK_METRICS, render.disk_metrics(rows))
    DISK_METRICS.touch()   # fresh for doctor also when the numbers stay the same
