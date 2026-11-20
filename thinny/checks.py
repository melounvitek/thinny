"""Isolation and health checks (thinny doctor).

Every check yields (level, message, fix) results. A check that crashes is reported as a
failure and the other checks still run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import dashboard as dash
from . import backup, host, instances, purelymail, render, settings, system, trace, update
from .registry import Instance, Registry
from .settings import MONITORING_PORTS, Settings

UPDATE_STATUS = system.STATE_DIR / "update-status.json"
INCUS_DROPIN = host.SYSTEMD / "incus.service.d/thinny.conf"
# Read before the managed scope is resolved, so an instance's own value beats the policy.
BYPASS_VARS = ("HERMES_MANAGED_DIR", "HERMES_BUNDLED_PLUGINS", "HERMES_ENABLE_PROJECT_PLUGINS")
FORWARD_FIX = f"""let the containers' traffic through your firewall's forward chain, e.g. in /etc/nftables.conf:
  iifname "thinny-*" accept
  oifname "thinny-*" ct state established,related accept
or with ufw: {host.UFW_FORWARD}
(table inet thinny does the filtering; see README, Prerequisites)"""


def ok(message: str):
    return "OK", message, ""


def warn(message: str, fix: str = ""):
    return "WARN", message, fix


def fail(message: str, fix: str = ""):
    return "FAIL", message, fix


def active(reg: Registry) -> list[Instance]:
    return [inst for inst in reg.current() if inst.state == "active"]


# ---------- host checks ----------

def install(s: Settings, reg: Registry):
    fix = "thinny setup"
    found = system.run(["incus", "profile", "show", render.PROFILE], check=False).returncode == 0
    yield (ok(f"Incus profile {render.PROFILE} present") if found
           else fail(f"Incus profile {render.PROFILE} missing", fix))
    yield (ok("incus.service starts only behind the firewall") if INCUS_DROPIN.exists()
           else fail(f"{INCUS_DROPIN} missing: containers could start without their firewall", fix))
    forwarding = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
    yield (ok("IPv4 forwarding on") if forwarding == "1"
           else fail("IPv4 forwarding off: containers have no network", "thinny config apply"))
    userns = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")  # Ubuntu only
    if userns.exists():
        yield (ok("AppArmor lets instances create user namespaces") if userns.read_text().strip() == "0"
               else fail("AppArmor keeps instances from creating user namespaces: their browser cannot start",
                         "thinny config apply"))
    stale = [inst.name for inst in active(reg) if not host.policy_current(instances.settings_for(s, inst), inst.name)]
    yield (warn(f"policy not as config.toml says: {', '.join(stale)}", "thinny config apply") if stale
           else ok(f"instance policies in {host.POLICIES} match config.toml"))
    if host.RELEASE_FILE.exists():
        yield ok(f"Hermes release of all instances: {host.RELEASE_FILE.read_text().split()[0]}")
    state = system.run(["systemctl", "is-enabled", "thinny-update.timer"], check=False).stdout.strip()
    yield (ok("thinny-update.timer enabled") if state == "enabled"
           else warn(f"thinny-update.timer is {state or 'missing'}",
                     "systemctl enable --now thinny-update.timer"))
    yield update_status(UPDATE_STATUS, dt.datetime.now(dt.UTC))


def tokens(s: Settings, reg: Registry):
    path = settings.SECRETS_ENV
    if path.exists():   # config edit --secrets keeps it so; another editor may not
        yield (ok(f"only root can read {path}") if not path.stat().st_mode & 0o077 else
               warn(f"others than root can read {path}, with thinny's tokens", f"chmod 600 {path}"))


def access_trace(s: Settings, reg: Registry):
    enabled, running = (system.run(["systemctl", query, "thinny-trace.service"], check=False).stdout.strip()
                        for query in ("is-enabled", "is-active"))
    yield (ok("thinny-trace.service traces admins' access to containers") if (enabled, running) == ("enabled", "active")
           else warn(f"thinny-trace.service is {enabled or 'missing'} and {running}: admins' access to containers is "
                     "not traced", "thinny config apply"))
    for folder in sorted(trace.UNSENT.glob("*")):
        rows = len(list(folder.glob("*.json")))
        if rows and folder.name in reg.instances and reg.get(folder.name).trace_sheet:   # others' rows just go
            yield warn(f"{folder.name}: {rows} rows of its access trace wait for its Google Sheet",
                       "check that the server reaches script.google.com: the trace tries again every 5 minutes, "
                       "and its journal (journalctl -u thinny-trace) says why they wait")


def mailboxes(s: Settings, reg: Registry):
    if not s.email_domain:
        return
    for host_name, port in ((purelymail.IMAP_HOST, 993), (purelymail.SMTP_HOST, 587)):   # Hermes's ports
        try:
            socket.create_connection((host_name, port), timeout=10).close()
        except OSError:   # Hermes then takes no mail either
            yield warn(f"this server cannot reach {host_name}:{port}: its instances' mail neither goes nor comes",
                       f"let outgoing TCP to port {port} through, also at your hosting provider (see README, Email)")
    try:
        purelymail.check_domain(s)
        found = purelymail.api("listUser")["users"]
    except system.CommandError as e:
        yield warn(f"mailboxes: {e}")
        return
    yield ok(f"Purelymail takes mail for {s.email_domain}")
    for inst in active(reg):
        if inst.mailbox and inst.mailbox not in found:
            yield warn(f"{inst.name}'s mailbox {inst.mailbox} is gone from Purelymail",
                       f"thinny instance email {inst.name}")


def update_status(path: Path, now: dt.datetime):
    """The last nightly update, from its status file: {"time": ISO time, "result": ..., "detail": ...}."""
    if not path.exists():
        return warn("no nightly update status yet (the update timer has not run)")
    status = json.loads(path.read_text(encoding="utf-8"))
    summary = f"last nightly update {status['time']}: {status['result']} {status.get('detail') or ''}".rstrip()
    if status["result"] == "FAILED":
        return fail(summary, "journalctl -u thinny-update.service")
    when = dt.datetime.fromisoformat(status["time"])
    if when.tzinfo is None:
        when = when.astimezone()  # local time
    if now - when > dt.timedelta(hours=48):
        return warn(f"{summary} (over 48 h ago)", "systemctl status thinny-update.timer")
    return ok(summary)


def backups(s: Settings, reg: Registry):
    if s.backup_target:
        yield backup_status(dt.datetime.now(dt.UTC))


def backup_status(now: dt.datetime):
    """The last backup, from its status file, and when one last worked: a failure keeps the nightly update waiting."""
    if not backup.KEY.exists():
        return fail("backups are on in config.toml, but not set up", "thinny backup init")
    if not backup.STATUS_FILE.exists():
        return warn("no backup yet (the nightly update takes one first)", "thinny backup")
    status = json.loads(backup.STATUS_FILE.read_text(encoding="utf-8"))
    summary = f"last backup {status['time']}: {status['result']} {status['detail']}"
    if status["result"] == "FAILED":
        return fail(f"{summary}\nthe nightly update waits for a backup that works",
                    "fix what it says, then: thinny backup")
    if now - dt.datetime.fromisoformat(status["last_success"]).astimezone() > dt.timedelta(hours=36):
        return fail(f"{summary} (over 36 h ago)", "systemctl status thinny-update.timer; then: thinny backup")
    return ok(summary)


def prerequisites(s: Settings, reg: Registry):
    """The admin's part (README): hardening, so WARN."""
    if shutil.which("sshd"):
        sshd = system.run(["sshd", "-T"], check=False).stdout.splitlines()
        # Keyboard-interactive asks for the password through PAM, so it must be off too.
        yield (ok("SSH accepts keys only")
               if "passwordauthentication no" in sshd and "kbdinteractiveauthentication no" in sshd
               else warn("SSH accepts passwords", "set PasswordAuthentication no and KbdInteractiveAuthentication no "
                         "in /etc/ssh/sshd_config.d/, then systemctl reload ssh"))

    ruleset = system.run(["nft", "list", "ruleset"], check=False).stdout.splitlines()
    yield (ok("an input firewall drops unexpected connections")
           if any("hook input" in line and "policy drop" in line for line in ruleset)
           else warn("no input firewall with policy drop: every listening port is open to the network",
                     "set up an input firewall that drops by default and allows SSH (nftables or ufw)"))
    # thinny turns IPv4 forwarding on for the containers.
    yield (ok("a forward firewall drops unexpected traffic")
           if any("hook forward" in line and "policy drop" in line for line in ruleset)
           else warn("no forward firewall with policy drop: with forwarding on, the server routes between its "
                     "networks", "set up a forward chain that drops by default; " + FORWARD_FIX))

    yield from unattended_upgrades(system.run(["apt-config", "dump"], check=False).stdout, s.update_time)
    # The containers share the server's kernel: a fixed one protects them only once it runs.
    yield (warn(f"a reboot is pending ({update.REBOOT_REQUIRED}): until then, upgrades that need it, such as a new "
                "kernel, protect nothing", "reboot at a quiet time, or wait for the automatic reboot")
           if update.REBOOT_REQUIRED.exists() else ok("no reboot pending"))


def unattended_upgrades(apt_dump: str, update_time: str):
    """Unattended upgrades on, and automatic reboots on, after the nightly Hermes update."""
    values = dict(re.findall(r'^(\S+) "(.*)";$', apt_dump, re.M))
    upgrades = values.get("APT::Periodic::Unattended-Upgrade") == "1"
    yield (ok("unattended upgrades on") if upgrades
           else warn("unattended upgrades are off: security updates wait for you",
                     "apt install unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades"))
    reboot = values.get("Unattended-Upgrade::Automatic-Reboot-Time", "")
    # apt's words for true
    if values.get("Unattended-Upgrade::Automatic-Reboot", "").lower() not in ("true", "yes", "on", "1", "with", "enable"):
        yield warn("automatic reboots are off: a new kernel protects the containers only after you reboot",
                   'set Unattended-Upgrade::Automatic-Reboot "true" and Unattended-Upgrade::Automatic-Reboot-Time '
                   f"later than {update_time} in /etc/apt/apt.conf.d/ (see README, Prerequisites)")
    elif re.fullmatch(r"\d\d:\d\d", reboot) and reboot <= update_time:
        yield warn(f"automatic reboots at {reboot}, not after the nightly Hermes update at {update_time}",
                   f"set Unattended-Upgrade::Automatic-Reboot-Time later than {update_time} in /etc/apt/apt.conf.d/")
    elif upgrades:   # unattended-upgrades reboots at the end of its run
        yield ok("automatic reboots on")


def firewall(s: Settings, reg: Registry):
    listed = system.run(["nft", "list", "table", "inet", "thinny"], check=False).stdout
    rendered = render.firewall(s.network_interface, list(s.blocked_networks))
    yield (ok("firewall table inet thinny loaded as config.toml says")
           if table_parts(listed) == table_parts(rendered)
           else fail("firewall table inet thinny is missing or not as config.toml says: containers may reach "
                     "each other, the host or blocked networks", "thinny config apply"))
    enabled, running = (system.run(["systemctl", query, "thinny-firewall.timer"], check=False).stdout.strip()
                        for query in ("is-enabled", "is-active"))
    yield (ok("thinny-firewall.timer checks every minute that the firewall table is loaded")
           if (enabled, running) == ("enabled", "active")
           else warn(f"thinny-firewall.timer is {enabled} and {running}: a flushed firewall table may stay gone",
                     "thinny config apply"))


def table_parts(text: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """The chains (name -> statements, in order) and sets (name -> elements) of an nft table, as we
    render it (a set on one line) or as `nft list table` prints it (a set over several lines)."""
    chains, sets, chain, set_name, depth = {}, {}, None, None, 0
    for line in text.splitlines():
        words = line.split()
        if words[:1] == ["set"] and set_name is None:
            set_name, set_depth, sets[words[1]] = words[1], depth, ""
        if set_name is not None:
            sets[set_name] += line
        elif words[:1] == ["chain"]:
            chain = chains.setdefault(words[1], [])
        elif words == ["}"]:
            chain = None
        elif chain is not None and words:
            chain.append(" ".join(words))
        depth += line.count("{") - line.count("}")
        if set_name is not None and depth == set_depth:
            set_name = None
    return chains, {name: " ".join(" ".join(re.findall(r"elements = \{[^}]*\}", body)).split())
                    for name, body in sets.items()}


def caddy(s: Settings, reg: Registry):
    """Caddy serves LAN instances, the dashboard on the LAN and, with a tunnel port, url instances."""
    lan = any(inst.access == "lan" for inst in active(reg)) or (host.dashboard_access() or (0, ""))[1] == "lan"
    tunnel = s.tunnel_port and any(inst.access == "url" for inst in active(reg))
    if not (lan or tunnel):
        return
    state = system.run(["systemctl", "is-active", "caddy"], check=False).stdout.strip()
    yield (ok("caddy active") if state == "active"
           else fail(f"caddy is {state}: Webapps it serves are unreachable", "systemctl restart caddy"))
    if tunnel:
        url = f"http://127.0.0.1:{s.tunnel_port}/"
        yield (ok(f"Caddy answers the tunnel at {url}") if system.http_status(url) != "000"
               else fail(f"nothing answers the tunnel at {url}", "thinny config apply"))
    if lan:
        ca = host.caddy_root_ca()
        yield (ok(f"Caddy root certificate for office PCs: {host.OFFICE_CA}")
               if ca.exists() and host.OFFICE_CA.exists() and host.OFFICE_CA.read_text() == ca.read_text()
               else warn(f"{host.OFFICE_CA} is missing or not Caddy's root certificate", "thinny config apply"))


def dashboard(s: Settings, reg: Registry):
    """With the dashboard on: its services, nothing of it beyond 127.0.0.1 but Caddy's and Tailscale's sites,
    Incus's metrics for Prometheus's certificate, Prometheus reading them and the instances' disk use (and
    subscription usage, when asked for)."""
    if not s.dashboard_enabled:
        return
    fix = "thinny config apply"
    published = host.dashboard_access()
    if published is None:
        yield warn("dashboard.enabled is true, but the dashboard is not installed", f"{fix} (on a terminal: it asks)")
        return
    if published != (s.dashboard_port, s.dashboard_access):
        yield warn("the dashboard is not as config.toml says", fix)
    units = [*dash.SERVICES, "thinny-metrics.timer"]
    states = system.run(["systemctl", "is-active", *units], check=False).stdout.split()
    down = [unit for unit, state in zip(units, states) if state != "active"]
    yield (fail(f"the dashboard's {', '.join(down)} not active", fix) if down
           else ok("the dashboard's Prometheus, node exporter and Grafana active"))
    ports = {published[0], *MONITORING_PORTS}
    exposed = []
    for line in system.run(["ss", "-Hltnp"], check=False).stdout.splitlines():
        local = line.split()[3]
        address, _, port = local.rpartition(":")
        if int(port) in ports and address not in ("127.0.0.1", "[::1]") and \
                not re.search(r'users:\(\("(caddy|tailscaled)"', line):
            exposed.append(local)
    yield (fail(f"the dashboard listens beyond 127.0.0.1: {', '.join(exposed)}", fix) if exposed
           else ok("the dashboard listens on 127.0.0.1 only, but where Caddy or Tailscale publish it"))
    address = system.run(["incus", "config", "get", "core.metrics_address"], check=False).stdout.strip()
    yield (ok(f"Incus serves its metrics at {address} to Prometheus's certificate")
           if address == dash.METRICS_ADDRESS and dash.CERT.exists() and dash.fingerprint() in dash.trusted()
           else fail(f"Incus serves no metrics at {dash.METRICS_ADDRESS} to Prometheus's certificate", fix))
    out = system.run(["curl", "-s", "-m", "5", "http://127.0.0.1:9090/api/v1/targets"], check=False, timeout=15).stdout
    try:
        targets = {target["labels"]["job"]: target for target in json.loads(out)["data"]["activeTargets"]}
    except (ValueError, KeyError, TypeError):
        targets = {}
    for job, what in (("incus", "Incus's metrics"), ("node", "the host's and thinny's")):
        target = targets.get(job) or {"health": "down", "lastError": "Prometheus does not answer"}
        if target["health"] == "up":
            yield ok(f"Prometheus reads {what}")
        elif target["health"] == "unknown":   # it reads every 30 s, the first time after it started
            yield warn(f"Prometheus has not read {what} yet", "wait half a minute")
        else:
            yield fail(f"Prometheus does not read {what}: {target['lastError']}", "journalctl -u prometheus")
    fresh = dash.DISK_METRICS.exists() and time.time() - dash.DISK_METRICS.stat().st_mtime < 15 * 60
    yield (ok("the instances' disk use is fresh") if fresh
           else warn("the instances' disk use is missing or older than 15 minutes", "journalctl -u thinny-metrics"))
    if s.dashboard_subscription_usage:   # asked for hourly; the file can be fresh without anybody's answer
        fresh = dash.USAGE_METRICS.exists() and time.time() - dash.USAGE_METRICS.stat().st_mtime < 2 * 3600
        yield (ok("the instances' subscription usage is fresh") if fresh
               else warn("the instances' subscription usage is missing or older than 2 hours",
                         "journalctl -u thinny-metrics"))
    code = system.http_status(f"http://127.0.0.1:{published[0]}/api/health")
    yield (ok(f"Grafana answers (HTTP {code})") if code == "200"
           else fail(f"Grafana does not answer at 127.0.0.1:{published[0]} (HTTP {code})", "journalctl -u grafana-server"))


# ---------- per-instance checks ----------

def container(s: Settings, inst: Instance):
    state = instances.container_state(inst.name)
    if state != "RUNNING":
        yield fail(f"{inst.name}: container is {state or 'missing'}", f"incus start {inst.name}")
        return
    yield ok(f"{inst.name}: container running")
    code = system.in_container(inst.name, ["curl", "-s", "-o", "/dev/null", "-m", "8", "-w", "%{http_code}",
                                           "https://deb.debian.org/"], check=False, timeout=30).stdout.strip()
    yield (ok(f"{inst.name}: container reaches the internet") if code[:1] in ("2", "3")
           else fail(f"{inst.name}: container cannot reach the internet (HTTP {code or '000'})",
                     host.UFW_FORWARD if host.ufw_lacks_forward() else FORWARD_FIX))


def webapp(s: Settings, inst: Instance):
    """The instance's frontend: the Webapp, or WebUI."""
    url = f"http://127.0.0.1:{inst.port}/"
    fix = f"thinny instance restart {inst.name}"
    frontend = instances.FRONTEND_NAMES[inst.frontend]
    yield (ok(f"{inst.name}: gateway active") if system.gateway_active(inst.name)
           else fail(f"{inst.name}: gateway not active", fix))
    code = system.http_status(url)
    yield (ok(f"{inst.name}: {frontend} answers (HTTP {code})") if code[0] in "23"
           else fail(f"{inst.name}: {frontend} does not answer at {url} (HTTP {code})", fix))
    if inst.frontend == "webui":
        if code[0] in "23":   # its users are root in their container
            status = system.run(["curl", "-s", "-m", "5", url + "api/auth/status"], check=False, timeout=15).stdout
            try:
                asks = json.loads(status).get("password_auth_enabled") is True
            except (ValueError, AttributeError):
                asks = False
            yield (ok(f"{inst.name}: WebUI asks for a password") if asks else
                   fail(f"{inst.name}: WebUI does not ask for a password", f"thinny instance reset-password {inst.name}"))
        return
    public_url = instances.webapp_settings(inst.name).get("public_url", "")
    yield (ok(f"{inst.name}: Webapp address {inst.url}") if public_url == inst.url
           else warn(f"{inst.name}: Webapp public_url is {public_url or 'not set'}, not {inst.url}",
                     "thinny config apply"))


def version(s: Settings, inst: Instance):
    tag, commit = host.release() or ("none", "")
    yield (ok(f"{inst.name}: Hermes {tag}") if instances.installed_commit(inst.name) == commit
           else warn(f"{inst.name}: Hermes is not at {tag}, the release all instances run (changed by its users, or an "
                     "update is unfinished); the next update puts it back"))
    if inst.frontend == "webui":
        tag, commit = host.release(host.WEBUI_RELEASE_FILE) or ("none", "")
        yield (ok(f"{inst.name}: WebUI {tag}") if instances.installed_commit(inst.name, webui=True) == commit
               else warn(f"{inst.name}: WebUI is not at {tag}, the release of the instances that use it (changed by its "
                         "users or its own updater)", f"thinny instance frontend {inst.name} webui"))


def built_ins(s: Settings, inst: Instance):
    """What every instance gets besides Hermes. Whether a plugin is on is its users' choice without the
    model lock, and the policy's with it."""
    fix = f"incus exec {inst.name} -- runuser -l {inst.name} -c "
    found = []
    if system.as_user(inst.name, ["claude", "--version"], check=False, timeout=60).returncode != 0:
        found.append(warn(f"{inst.name}: Claude Code is missing (the Claude subscription plugin runs it)",
                          fix + f"'curl -fsSL {instances.CLAUDE_INSTALLER} | bash'"))
    installed = instances.plugin_states(inst.name)
    for name, args in render.PLUGINS:
        if name not in installed:
            found.append(warn(f"{inst.name}: plugin {name} is missing",
                              fix + f"'hermes plugins install {' '.join(args)} --yes-deps --enable'"))
    return found or [ok(f"{inst.name}: Claude Code and the built-in plugins installed")]


def bypass(s: Settings, inst: Instance):
    home = instances.home(inst.name)
    unit = home.parent / ".config/systemd/user/hermes-gateway.service"
    own_guard = instances.settings_for(s, inst).model_lock and system.as_user(
        inst.name, ["test", "-e", str(home / "plugins/model-guard")], check=False, timeout=30).returncode == 0
    # WebUI runs the agent in its own process
    webui = "".join(system.read_user_file(inst.name, str(instances.webui_files(inst.name)[f]))
                    for f in ("unit", "password")) if inst.frontend == "webui" else ""
    yield from bypasses(inst.name, system.read_user_file(inst.name, str(home / ".env")),
                        system.read_user_file(inst.name, str(unit)), own_guard,
                        str(home / "hermes-agent/.hermes/bin/hermes"), webui)


def bypasses(name: str, env_text: str, unit_text: str, own_guard: bool, gateway_binary: str,
             webui_text: str = "") -> list:
    """Ways around the central policy in an instance's own files (`webui_text`: WebUI's unit and password file).
    A guardrail (its users are root in its container), so WARN. Reports variable names only, never file
    contents."""
    found = []
    names = [v for v in BYPASS_VARS if re.search(rf"^\s*(export\s+)?{v}\s*=", env_text, re.M)]
    if names:
        found.append(warn(f"{name}: ~/.hermes/.env sets {', '.join(names)}", f"remove them, as {name}"))
    if own_guard:
        found.append(warn(f"{name}: ~/.hermes/plugins/model-guard replaces the server's model guard",
                          f"incus exec {name} -- rm -r /home/{name}/.hermes/plugins/model-guard"))
    names = [v for v in BYPASS_VARS if v in unit_text]
    if names:
        found.append(warn(f"{name}: its hermes-gateway.service sets {', '.join(names)}"))
    commands = {c for c in re.findall(r'^\s*ExecStart\s*=\s*[-@:+!]*"?([^\s"]*)', unit_text, re.M) if c}
    if unit_text and commands != {gateway_binary}:
        found.append(warn(f"{name}: its hermes-gateway.service does not start {gateway_binary}"))
    names = [v for v in BYPASS_VARS if v in webui_text]
    if names:
        found.append(warn(f"{name}: its hermes-webui.service sets {', '.join(names)}"))
    return found or [ok(f"{name}: no way around the policy found")]


# ---------- status: the quick look ----------

def overview(s: Settings, reg: Registry):
    yield ok(f"Hermes release of all instances: {(host.release() or ('none yet',))[0]}")
    if webui := host.release(host.WEBUI_RELEASE_FILE):
        yield ok(f"WebUI release of the instances that use it: {webui[0]}")
    yield update_status(UPDATE_STATUS, dt.datetime.now(dt.UTC))
    yield from backups(s, reg)


# ---------- runner ----------

CHECKS = [install, tokens, backups, access_trace, mailboxes, prerequisites, firewall, caddy, dashboard]
INSTANCE_CHECKS = [container, webapp, version, built_ins, bypass]
# Checks at once. An instance's start Hermes's command line in its container, which takes CPU and memory.
WORKERS = 4


def run(s: Settings, reg: Registry) -> int:
    """Every check (doctor); 1 when anything failed."""
    return run_checks(s, reg, CHECKS, INSTANCE_CHECKS)


def status(s: Settings, reg: Registry) -> int:
    """The quick look: no internet, Webapp or policy probes; 1 when anything failed."""
    states, error = {}, ""
    try:
        if active(reg):
            states = instances.container_states()   # every container's in one incus list
    except Exception as e:   # each instance's check fails with it
        error = f"{type(e).__name__}: {e}"

    def running(s: Settings, inst: Instance):
        state = states.get(inst.name)
        if error:
            yield fail(f"{inst.name}: container state unknown: {error}")
        elif state != "RUNNING":
            yield fail(f"{inst.name}: container is {state or 'missing'}", f"incus start {inst.name}")
        elif system.gateway_active(inst.name):
            yield ok(f"{inst.name}: running, gateway active")
        else:
            yield fail(f"{inst.name}: gateway not active", f"thinny instance restart {inst.name}")

    return run_checks(s, reg, [overview], [running])


def run_checks(s: Settings, reg: Registry, checks, instance_checks) -> int:
    """Print every result, in order; 1 when anything failed. Each host check, and each instance's checks one after
    another, run alongside the others, WORKERS at a time; their results show once they and those before them are
    done."""
    groups = [[(check.__name__, check, (s, reg))] for check in checks]
    groups += [[(f"{check.__name__} {inst.name}", check, (s, inst)) for check in instance_checks]
               for inst in active(reg)]
    failed = False
    with ThreadPoolExecutor(WORKERS) as pool:
        for results in pool.map(collect, groups):
            for level, message, fix in results:
                report(level, message, fix)
                failed |= level == "FAIL"
    return 1 if failed else 0


def collect(jobs) -> list:
    """The results of these checks, one after another; a check that crashes is a failure, and the others still run."""
    results = []
    for name, check, args in jobs:
        try:
            results.extend(check(*args))
        except Exception as e:
            results.append(fail(f"{name} crashed: {type(e).__name__}: {e}"))
    return results


def report(level: str, message: str, fix: str = "") -> None:
    """A result, printable: a message may hold what an instance's container said."""
    print(f"{level:<4} " + "\n     ".join(map(system.printable, message.splitlines())))
    if fix:
        print(f"     fix: {fix}".replace("\n", "\n" + " " * 10))
