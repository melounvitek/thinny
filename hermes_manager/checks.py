"""Isolation and health checks (hermes-manager check).

Every check yields (level, message, fix) results. A check that crashes is reported as a
failure and the other checks still run.
"""

from __future__ import annotations

import datetime as dt
import grp
import ipaddress
import json
import re
import shutil
from pathlib import Path

from . import host, people, render, system
from .registry import Person, Registry
from .settings import Settings

UPDATE_STATUS = system.STATE_DIR / "update-status.json"
GATEWAY_BINARY = f"{render.CHECKOUT}/.hermes/bin/hermes"
# Read before the managed scope is resolved, so a person's own value beats the policy.
BYPASS_VARS = ("HERMES_MANAGED_DIR", "HERMES_BUNDLED_PLUGINS", "HERMES_ENABLE_PROJECT_PLUGINS")
TAILSCALE_SOCKET = Path("/run/tailscale/tailscaled.sock")
TAILSCALE_FIX = """create /etc/systemd/system/tailscaled.service.d/10-root-only.conf with:
  [Service]
  RuntimeDirectoryMode=0700
  ExecStartPost=/bin/chmod 0600 /run/tailscale/tailscaled.sock
systemctl daemon-reload && systemctl restart tailscaled"""


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
    mode = host.INSTALL.stat().st_mode & 0o777
    yield (ok(f"{host.INSTALL} is mode 711") if mode == 0o711
           else fail(f"{host.INSTALL} is mode {mode:o}, not 711: people's hermes fails", "chmod 711 /opt/hermes"))
    for path in (Path(system.HERMES), Path("/usr/local/bin/hermes-podman")):
        yield ok(f"{path} present") if path.exists() else fail(f"{path} missing", "hermes-manager install")
    yield (ok("Webapp built") if host.WEBAPP_INDEX.exists()
           else fail(f"Webapp not built ({host.WEBAPP_INDEX} missing)", "hermes-manager install"))
    for plugin in ["model-guard"] + ([s.language_pack] if s.language_pack else []):
        path = host.CHECKOUT / "plugins" / plugin
        yield ok(f"plugin {plugin} present") if path.is_dir() else fail(f"{path} missing", "hermes-manager apply")

    stale = [path.name for path, text in host.managed_files(s).items()
             if not path.is_file() or path.read_text(encoding="utf-8") != text]
    yield (ok(f"{host.MANAGED} matches site.toml") if not stale
           else warn(f"{host.MANAGED}: {', '.join(stale)} not as site.toml says", "hermes-manager apply"))

    state = system.run(["systemctl", "is-active", "hermes-manager-firewall.service"], check=False).stdout.strip()
    yield (ok("hermes-manager-firewall.service active") if state == "active"
           else fail(f"hermes-manager-firewall.service is {state}", "systemctl restart hermes-manager-firewall"))
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
    """The admin's part (README): FAIL for what person isolation rests on, WARN for hardening."""
    if proc_hidden(Path("/proc/mounts").read_text()):
        yield ok("/proc hides other accounts' processes (hidepid)")
    else:
        try:
            gid, create = grp.getgrnam("proc-viewers").gr_gid, ""
        except KeyError:
            gid, create = "<its gid>", "groupadd --system proc-viewers\n"
        yield fail("/proc is mounted without hidepid: every account sees everyone's processes and command lines",
                   f"{create}add to /etc/fstab: proc /proc proc defaults,hidepid=invisible,gid={gid} 0 0\n"
                   "create /etc/systemd/system/systemd-logind.service.d/hidepid.conf with:\n"
                   "  [Service]\n"
                   "  SupplementaryGroups=proc-viewers\n"
                   "systemctl daemon-reload && systemctl restart systemd-logind && mount -o remount /proc")

    if shutil.which("tailscale"):
        if not TAILSCALE_SOCKET.exists():
            yield warn(f"Tailscale is installed but {TAILSCALE_SOCKET} is missing (tailscaled not running?)")
        elif socket_private(TAILSCALE_SOCKET.stat().st_mode, TAILSCALE_SOCKET.parent.stat().st_mode):
            yield ok("Tailscale socket is root-only")
        else:
            yield fail("Tailscale socket is open to every account: they can read the tailnet and everyone's "
                       "Webapp URLs", TAILSCALE_FIX)

    if shutil.which("sshd"):
        sshd = system.run(["sshd", "-T"], check=False).stdout.splitlines()
        # Keyboard-interactive asks for the password through PAM, so it must be off too.
        yield (ok("SSH accepts keys only")
               if "passwordauthentication no" in sshd and "kbdinteractiveauthentication no" in sshd
               else warn("SSH accepts passwords", "set PasswordAuthentication no and KbdInteractiveAuthentication no "
                         "in /etc/ssh/sshd_config.d/, then systemctl reload ssh"))

    ruleset = system.run(["nft", "list", "ruleset"], check=False).stdout
    yield (ok("an input firewall drops unexpected connections")
           if any("hook input" in line and "policy drop" in line for line in ruleset.splitlines())
           else warn("no input firewall with policy drop: every listening port is open to the network",
                     "set up an input firewall that drops by default and allows SSH (nftables or ufw)"))

    yield from unattended_upgrades(system.run(["apt-config", "dump"], check=False).stdout, s.update_time)


def proc_hidden(mounts: str) -> bool:
    """True when the /proc mount (the last one wins) hides other accounts' processes."""
    options = [line.split()[3].split(",") for line in mounts.splitlines() if line.split()[1:3] == ["/proc", "proc"]]
    return bool(options) and not {"hidepid=invisible", "hidepid=2"}.isdisjoint(options[-1])


def socket_private(socket_mode: int, dir_mode: int) -> bool:
    """Only root can reach the socket: no group/other bits on it or on its directory."""
    return socket_mode & 0o077 == 0 or dir_mode & 0o077 == 0


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
    fix = "systemctl restart hermes-manager-firewall"
    table = system.run(["nft", "list", "table", "inet", "hermes_manager"], check=False)
    if table.returncode != 0:
        yield fail("firewall table inet hermes_manager not loaded: people can reach each other's programs", fix)
        return
    yield ok("firewall table inet hermes_manager loaded")
    slices = set(system.slice_uids())
    for p in active(reg):
        if p.uid in slices:
            rule = f"user-{p.uid}.slice"
            yield (ok(f"{p.name}: firewall guards {rule}") if rule in table.stdout
                   else fail(f"{p.name}: no firewall rule for {rule}: other accounts can reach {p.name}'s programs",
                             fix))


def lan_access(s: Settings, reg: Registry):
    if not any(p.access == "lan" for p in active(reg)):
        return
    state = system.run(["systemctl", "is-active", "caddy"], check=False).stdout.strip()
    yield (ok("caddy active (office-network access)") if state == "active"
           else fail(f"caddy is {state}: office-network Webapps are unreachable", "systemctl restart caddy"))
    if host.caddy_root_ca().exists():
        yield ok(f"Caddy root certificate for office PCs: {host.caddy_root_ca()}")


def listeners(s: Settings, reg: Registry):
    owners = {p.uid: p.name for p in reg.people.values() if p.uid is not None}
    exposed = [(owners[uid], program, local)
               for local, program, pids in exposed_listeners(system.run(["ss", "-ltnpH"]).stdout)
               for uid in {pid_uid(pid) for pid in pids} if uid in owners]
    if not exposed:
        yield ok("no person's program listens on a network address")
    for name, program, local in exposed:
        yield warn(f"{name}: {program} listens on {local}, open to the network (the firewall guards loopback only)")


def exposed_listeners(ss_output: str) -> list[tuple[str, str, list[int]]]:
    """(local address, program, pids) of the TCP listeners not bound to loopback, from `ss -ltnpH`."""
    found = []
    for line in ss_output.splitlines():
        cols = line.split(maxsplit=5)  # State Recv-Q Send-Q Local Peer Process
        if len(cols) < 6:
            continue  # no owning process (kernel sockets)
        local, process = cols[3], cols[5]
        address = local.rpartition(":")[0].partition("%")[0].strip("[]")
        if address != "*" and ipaddress.ip_address(address).is_loopback:
            continue
        program = re.search(r'\(\("([^"]*)"', process)
        found.append((local, program.group(1) if program else "?", [int(p) for p in re.findall(r"pid=(\d+)", process)]))
    return found


def pid_uid(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return None  # exited since ss listed it
    return int(re.search(r"^Uid:\s+(\d+)", status, re.M).group(1))


# ---------- per-person checks ----------

def webapp(p: Person):
    url = f"http://127.0.0.1:{p.port}/"
    other = system.run(["runuser", "-u", "nobody", "--", "curl", "-s", "-o", "/dev/null", "-m", "4",
                        "-w", "%{http_code}", url], check=False, timeout=20).stdout.strip() or "000"
    yield (ok(f"{p.name}: Webapp port {p.port} closed to other accounts") if other == "000"
           else fail(f"{p.name}: Webapp port {p.port} answers another account (HTTP {other})",
                     "systemctl restart hermes-manager-firewall"))
    fix = f"hermes-manager restart {p.name}"
    yield (ok(f"{p.name}: gateway active") if system.gateway_active(p.name)
           else fail(f"{p.name}: gateway not active", fix))
    code = system.http_status(url)
    yield (ok(f"{p.name}: Webapp answers (HTTP {code})") if code[0] in "23"
           else fail(f"{p.name}: Webapp does not answer at {url} (HTTP {code})", fix))
    public_url = people.webapp_settings(p.name).get("public_url", "")
    yield (ok(f"{p.name}: Webapp address {p.url}") if public_url == p.url
           else warn(f"{p.name}: Webapp public_url is {public_url or 'not set'}, not {p.url}", "hermes-manager apply"))


def environment(p: Person):
    """After an update, a person's Python environment must follow the new dependency lock."""
    res = people.hermes(p.name, ["config", "path"], check=False, timeout=600)
    stale = "out of sync" in res.stdout + res.stderr
    yield (warn(f"{p.name}: Hermes environment out of sync with the installed release",
                f"runuser -l {p.name} -c 'hermes pm install --extra all' && hermes-manager restart {p.name}")
           if stale else ok(f"{p.name}: Hermes environment current"))


def bypass(p: Person):
    home = people.home(p.name)

    def read(path):  # as the person: as root, a planted symlink or FIFO could redirect or hang the read
        return system.as_user(p.name, ["cat", str(path)], check=False, timeout=30).stdout

    unit = home.parent / ".config/systemd/user/hermes-gateway.service"
    own_guard = system.as_user(p.name, ["test", "-e", str(home / "plugins/model-guard")],
                               check=False, timeout=30).returncode == 0
    yield from bypasses(p.name, read(home / ".env"), read(unit), own_guard)


def bypasses(name: str, env_text: str, unit_text: str, own_guard: bool) -> list:
    """Ways around the central policy in a person's own files (a guardrail, so WARN).
    Reports variable names only, never file contents."""
    found = []
    names = [v for v in BYPASS_VARS if re.search(rf"^\s*(export\s+)?{v}\s*=", env_text, re.M)]
    if names:
        found.append(warn(f"{name}: ~/.hermes/.env sets {', '.join(names)}", f"remove them, as {name}"))
    if own_guard:
        found.append(warn(f"{name}: ~/.hermes/plugins/model-guard replaces the server's model guard",
                          f"runuser -l {name} -c 'rm -r ~/.hermes/plugins/model-guard'"))
    names = [v for v in BYPASS_VARS if v in unit_text]
    if names:
        found.append(warn(f"{name}: their hermes-gateway.service sets {', '.join(names)}"))
    commands = {c for c in re.findall(r'^\s*ExecStart\s*=\s*[-@:+!]*"?([^\s"]*)', unit_text, re.M) if c}
    if unit_text and commands != {GATEWAY_BINARY}:
        found.append(warn(f"{name}: their hermes-gateway.service does not start {GATEWAY_BINARY}"))
    return found or [ok(f"{name}: no way around the policy found")]


def sandboxes(p: Person):
    names = [n for n in system.as_user(p.name, ["podman", "ps", "--format", "{{.Names}}"], timeout=60).stdout.split()
             if n.startswith("hermes-")]
    if not names:
        yield ok(f"{p.name}: no sandbox running")
    for name in names:
        code = system.as_user(p.name, ["podman", "exec", name, "curl", "-s", "-o", "/dev/null", "-m", "8",
                                       "-w", "%{http_code}", "https://example.com/"],
                              check=False, timeout=30).stdout.strip() or "000"
        yield (ok(f"{p.name}: sandbox {name} reaches the internet") if code[0] in "23"
               else warn(f"{p.name}: sandbox {name} cannot reach the internet (HTTP {code})",
                         f"hermes-manager restart {p.name}"))
        conmon = system.as_user(p.name, ["podman", "inspect", "--format", "{{.State.ConmonPid}}", name],
                                timeout=30).stdout.strip()
        yield (ok(f"{p.name}: sandbox {name} runs in hermes-sandboxes.slice")
               if "hermes-sandboxes.slice" in Path(f"/proc/{conmon}/cgroup").read_text()
               else warn(f"{p.name}: sandbox {name} runs outside hermes-sandboxes.slice: "
                         "its network dies when the gateway restarts"))


# ---------- runner ----------

CHECKS = [install, prerequisites, firewall, lan_access, listeners]
PERSON_CHECKS = [webapp, environment, bypass, sandboxes]


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
