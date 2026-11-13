"""Isolation and health checks (hermes-manager check).

Every check yields (level, message, fix) results. A check that crashes is reported as a
failure and the other checks still run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path

from . import host, people, render, system
from .registry import Person, Registry
from .settings import Settings

UPDATE_STATUS = system.STATE_DIR / "update-status.json"
INCUS_DROPIN = host.SYSTEMD / "incus.service.d/hermes-manager.conf"
# Read before the managed scope is resolved, so a person's own value beats the policy.
BYPASS_VARS = ("HERMES_MANAGED_DIR", "HERMES_BUNDLED_PLUGINS", "HERMES_ENABLE_PROJECT_PLUGINS")
FORWARD_FIX = """let the containers' traffic through your firewall's forward chain, e.g. in /etc/nftables.conf:
  iifname "hm-*" accept
  oifname "hm-*" ct state established,related accept
or with ufw: ufw route allow in on hm-+
(table inet hermes_manager does the filtering; see README, Requirements)"""


def ok(message: str):
    return "OK", message, ""


def warn(message: str, fix: str = ""):
    return "WARN", message, fix


def fail(message: str, fix: str = ""):
    return "FAIL", message, fix


def active(reg: Registry) -> list[Person]:
    return [p for p in reg.current() if p.state == "active"]


# ---------- host checks ----------

def install(s: Settings, reg: Registry):
    fix = "hermes-manager install"
    for kind, name in (("storage", render.POOL), ("profile", render.PROFILE)):
        found = system.run(["incus", kind, "show", name], check=False).returncode == 0
        yield ok(f"Incus {kind} {name} present") if found else fail(f"Incus {kind} {name} missing", fix)
    yield (ok("incus.service starts only behind the firewall") if INCUS_DROPIN.exists()
           else fail(f"{INCUS_DROPIN} missing: containers could start without their firewall", fix))
    forwarding = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
    yield (ok("IPv4 forwarding on") if forwarding == "1"
           else fail("IPv4 forwarding off: containers have no network", "hermes-manager apply"))
    userns = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")  # Ubuntu only
    if userns.exists():
        yield (ok("AppArmor lets people create user namespaces") if userns.read_text().strip() == "0"
               else fail("AppArmor keeps people from creating user namespaces: their browser cannot start",
                         "hermes-manager apply"))
    yield (ok(f"{host.MANAGED} matches site.toml") if host.managed_scope_current(s)
           else warn(f"{host.MANAGED} is not as site.toml says", "hermes-manager apply"))
    if host.RELEASE_FILE.exists():
        yield ok(f"Hermes release for everyone: {host.RELEASE_FILE.read_text().split()[0]}")
    state = system.run(["systemctl", "is-enabled", "hermes-manager-update.timer"], check=False).stdout.strip()
    yield (ok("hermes-manager-update.timer enabled") if state == "enabled"
           else warn(f"hermes-manager-update.timer is {state or 'missing'}",
                     "systemctl enable --now hermes-manager-update.timer"))
    yield update_status(UPDATE_STATUS, dt.datetime.now(dt.UTC))


def update_status(path: Path, now: dt.datetime):
    """The last nightly update, from its status file: {"time": ISO time, "result": ..., "detail": ...}."""
    if not path.exists():
        return warn("no nightly update status yet (the update timer has not run)")
    status = json.loads(path.read_text(encoding="utf-8"))
    summary = f"last nightly update {status['time']}: {status['result']} {status.get('detail') or ''}".rstrip()
    if status["result"] == "FAILED":
        return fail(summary, "journalctl -u hermes-manager-update.service")
    when = dt.datetime.fromisoformat(status["time"])
    if when.tzinfo is None:
        when = when.astimezone()  # local time
    if now - when > dt.timedelta(hours=48):
        return warn(f"{summary} (over 48 h ago)", "systemctl status hermes-manager-update.timer")
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
    # hermes-manager turns IPv4 forwarding on for the containers.
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
    listed = system.run(["nft", "list", "table", "inet", "hermes_manager"], check=False).stdout
    rendered = render.firewall(s.network_interface, list(s.blocked_networks))
    yield (ok("firewall table inet hermes_manager loaded as site.toml says")
           if table_parts(listed) == table_parts(rendered)
           else fail("firewall table inet hermes_manager is missing or not as site.toml says: containers may reach "
                     "each other, the host or blocked networks", "hermes-manager apply"))


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


def lan_access(s: Settings, reg: Registry):
    if not any(p.access == "lan" for p in active(reg)):
        return
    state = system.run(["systemctl", "is-active", "caddy"], check=False).stdout.strip()
    yield (ok("caddy active (office-network access)") if state == "active"
           else fail(f"caddy is {state}: office-network Webapps are unreachable", "systemctl restart caddy"))
    if host.caddy_root_ca().exists():
        yield ok(f"Caddy root certificate for office PCs: {host.caddy_root_ca()}")


# ---------- per-person checks ----------

def container(p: Person):
    state = people.container_state(p.name)
    if state != "RUNNING":
        yield fail(f"{p.name}: container is {state or 'missing'}", f"incus start {p.name}")
        return
    yield ok(f"{p.name}: container running")
    code = system.in_container(p.name, ["curl", "-s", "-o", "/dev/null", "-m", "8", "-w", "%{http_code}",
                                        "https://deb.debian.org/"], check=False, timeout=30).stdout.strip()
    yield (ok(f"{p.name}: container reaches the internet") if code[:1] in ("2", "3")
           else fail(f"{p.name}: container cannot reach the internet (HTTP {code or '000'})", FORWARD_FIX))


def webapp(p: Person):
    url = f"http://127.0.0.1:{p.port}/"
    fix = f"hermes-manager restart {p.name}"
    yield (ok(f"{p.name}: gateway active") if system.gateway_active(p.name)
           else fail(f"{p.name}: gateway not active", fix))
    code = system.http_status(url)
    yield (ok(f"{p.name}: Webapp answers (HTTP {code})") if code[0] in "23"
           else fail(f"{p.name}: Webapp does not answer at {url} (HTTP {code})", fix))
    public_url = people.webapp_settings(p.name).get("public_url", "")
    yield (ok(f"{p.name}: Webapp address {p.url}") if public_url == p.url
           else warn(f"{p.name}: Webapp public_url is {public_url or 'not set'}, not {p.url}", "hermes-manager apply"))


def version(p: Person):
    tag, commit = host.release() or ("none", "")
    yield (ok(f"{p.name}: Hermes {tag}") if people.installed_commit(p.name) == commit
           else warn(f"{p.name}: Hermes is not at {tag}, the release everyone runs (changed by them, or an "
                     "update is unfinished); the next update puts it back"))


def bypass(p: Person):
    home = people.home(p.name)
    unit = home.parent / ".config/systemd/user/hermes-gateway.service"
    own_guard = system.as_user(p.name, ["test", "-e", str(home / "plugins/model-guard")],
                               check=False, timeout=30).returncode == 0
    yield from bypasses(p.name, system.read_user_file(p.name, str(home / ".env")),
                        system.read_user_file(p.name, str(unit)), own_guard,
                        str(home / "hermes-agent/.hermes/bin/hermes"))


def bypasses(name: str, env_text: str, unit_text: str, own_guard: bool, gateway_binary: str) -> list:
    """Ways around the central policy in a person's own files. A guardrail (they are root in their
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
        found.append(warn(f"{name}: their hermes-gateway.service sets {', '.join(names)}"))
    commands = {c for c in re.findall(r'^\s*ExecStart\s*=\s*[-@:+!]*"?([^\s"]*)', unit_text, re.M) if c}
    if unit_text and commands != {gateway_binary}:
        found.append(warn(f"{name}: their hermes-gateway.service does not start {gateway_binary}"))
    return found or [ok(f"{name}: no way around the policy found")]


# ---------- runner ----------

CHECKS = [install, prerequisites, firewall, lan_access]
PERSON_CHECKS = [container, webapp, version, bypass]


def run(s: Settings, reg: Registry) -> int:
    """Print every result; 1 when anything failed."""
    jobs = [(check.__name__, check, (s, reg)) for check in CHECKS]
    jobs += [(f"{check.__name__} {p.name}", check, (p,)) for p in active(reg) for check in PERSON_CHECKS]
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
