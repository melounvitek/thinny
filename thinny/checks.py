"""Isolation and health checks (thinny doctor).

Every check yields (level, message, fix) results. A check that crashes is reported as a
failure and the other checks still run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path

from . import host, instances, render, system, trace
from .registry import Instance, Registry
from .settings import Settings

UPDATE_STATUS = system.STATE_DIR / "update-status.json"
INCUS_DROPIN = host.SYSTEMD / "incus.service.d/thinny.conf"
# Read before the managed scope is resolved, so an instance's own value beats the policy.
BYPASS_VARS = ("HERMES_MANAGED_DIR", "HERMES_BUNDLED_PLUGINS", "HERMES_ENABLE_PROJECT_PLUGINS")
FORWARD_FIX = """let the containers' traffic through your firewall's forward chain, e.g. in /etc/nftables.conf:
  iifname "thinny-*" accept
  oifname "thinny-*" ct state established,related accept
or with ufw: ufw route allow in on thinny-+
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
    for kind, name in (("storage", render.POOL), ("profile", render.PROFILE)):
        found = system.run(["incus", kind, "show", name], check=False).returncode == 0
        yield ok(f"Incus {kind} {name} present") if found else fail(f"Incus {kind} {name} missing", fix)
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
    yield (warn(f"policy not as site.toml says: {', '.join(stale)}", "thinny config apply") if stale
           else ok(f"instance policies in {host.POLICIES} match site.toml"))
    if host.RELEASE_FILE.exists():
        yield ok(f"Hermes release of all instances: {host.RELEASE_FILE.read_text().split()[0]}")
    state = system.run(["systemctl", "is-enabled", "thinny-update.timer"], check=False).stdout.strip()
    yield (ok("thinny-update.timer enabled") if state == "enabled"
           else warn(f"thinny-update.timer is {state or 'missing'}",
                     "systemctl enable --now thinny-update.timer"))
    yield update_status(UPDATE_STATUS, dt.datetime.now(dt.UTC))


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


def unattended_upgrades(apt_dump: str, update_time: str):
    """Unattended upgrades on, and automatic reboots (if any) after the nightly Hermes update."""
    values = dict(re.findall(r'^(\S+) "(.*)";$', apt_dump, re.M))
    if values.get("APT::Periodic::Unattended-Upgrade") != "1":
        yield warn("unattended upgrades are off: security updates wait for you",
                   "apt install unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades")
        return
    yield ok("unattended upgrades on")
    reboot = values.get("Unattended-Upgrade::Automatic-Reboot-Time", "")
    if values.get("Unattended-Upgrade::Automatic-Reboot") == "true" and re.fullmatch(r"\d\d:\d\d", reboot) \
            and reboot <= update_time:
        yield warn(f"automatic reboots at {reboot}, not after the nightly Hermes update at {update_time}",
                   f"set Unattended-Upgrade::Automatic-Reboot-Time later than {update_time} in /etc/apt/apt.conf.d/")


def firewall(s: Settings, reg: Registry):
    listed = system.run(["nft", "list", "table", "inet", "thinny"], check=False).stdout
    rendered = render.firewall(s.network_interface, list(s.blocked_networks))
    yield (ok("firewall table inet thinny loaded as site.toml says")
           if table_parts(listed) == table_parts(rendered)
           else fail("firewall table inet thinny is missing or not as site.toml says: containers may reach "
                     "each other, the host or blocked networks", "thinny config apply"))


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
    """Caddy serves LAN instances and, with a tunnel port, url instances."""
    lan = any(inst.access == "lan" for inst in active(reg))
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
           else fail(f"{inst.name}: container cannot reach the internet (HTTP {code or '000'})", FORWARD_FIX))


def webapp(s: Settings, inst: Instance):
    url = f"http://127.0.0.1:{inst.port}/"
    fix = f"thinny instance restart {inst.name}"
    yield (ok(f"{inst.name}: gateway active") if system.gateway_active(inst.name)
           else fail(f"{inst.name}: gateway not active", fix))
    code = system.http_status(url)
    yield (ok(f"{inst.name}: Webapp answers (HTTP {code})") if code[0] in "23"
           else fail(f"{inst.name}: Webapp does not answer at {url} (HTTP {code})", fix))
    public_url = instances.webapp_settings(inst.name).get("public_url", "")
    yield (ok(f"{inst.name}: Webapp address {inst.url}") if public_url == inst.url
           else warn(f"{inst.name}: Webapp public_url is {public_url or 'not set'}, not {inst.url}",
                     "thinny config apply"))


def version(s: Settings, inst: Instance):
    tag, commit = host.release() or ("none", "")
    yield (ok(f"{inst.name}: Hermes {tag}") if instances.installed_commit(inst.name) == commit
           else warn(f"{inst.name}: Hermes is not at {tag}, the release all instances run (changed by its users, or an "
                     "update is unfinished); the next update puts it back"))


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
    yield from bypasses(inst.name, system.read_user_file(inst.name, str(home / ".env")),
                        system.read_user_file(inst.name, str(unit)), own_guard,
                        str(home / "hermes-agent/.hermes/bin/hermes"))


def bypasses(name: str, env_text: str, unit_text: str, own_guard: bool, gateway_binary: str) -> list:
    """Ways around the central policy in an instance's own files. A guardrail (its users are root in its
    container), so WARN. Reports variable names only, never file contents."""
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
    return found or [ok(f"{name}: no way around the policy found")]


# ---------- status: the quick look ----------

def overview(s: Settings, reg: Registry):
    yield ok(f"Hermes release of all instances: {(host.release() or ('none yet',))[0]}")
    yield update_status(UPDATE_STATUS, dt.datetime.now(dt.UTC))


def running(s: Settings, inst: Instance):
    state = instances.container_state(inst.name)
    if state != "RUNNING":
        yield fail(f"{inst.name}: container is {state or 'missing'}", f"incus start {inst.name}")
    elif system.gateway_active(inst.name):
        yield ok(f"{inst.name}: running, gateway active")
    else:
        yield fail(f"{inst.name}: gateway not active", f"thinny instance restart {inst.name}")


# ---------- runner ----------

CHECKS = [install, access_trace, prerequisites, firewall, caddy]
INSTANCE_CHECKS = [container, webapp, version, built_ins, bypass]


def run(s: Settings, reg: Registry) -> int:
    """Every check (doctor); 1 when anything failed."""
    return run_checks(s, reg, CHECKS, INSTANCE_CHECKS)


def status(s: Settings, reg: Registry) -> int:
    """The quick look: no internet, Webapp or policy probes; 1 when anything failed."""
    return run_checks(s, reg, [overview], [running])


def run_checks(s: Settings, reg: Registry, checks, instance_checks) -> int:
    """Print every result; 1 when anything failed."""
    jobs = [(check.__name__, check, (s, reg)) for check in checks]
    jobs += [(f"{check.__name__} {inst.name}", check, (s, inst)) for inst in active(reg) for check in instance_checks]
    failed = False
    for name, check, args in jobs:
        try:
            for level, message, fix in check(*args):
                report(level, message, fix)
                failed |= level == "FAIL"
        except Exception as e:
            report("FAIL", f"{name} crashed: {type(e).__name__}: {e}")
            failed = True
    return 1 if failed else 0


def report(level: str, message: str, fix: str = "") -> None:
    print(f"{level:<4} {message}".replace("\n", "\n" + " " * 5))
    if fix:
        print(f"     fix: {fix}".replace("\n", "\n" + " " * 10))
