import itertools
import subprocess
import sys

import pytest

from hermes_manager import host, people, system
from hermes_manager.registry import Person, Registry
from hermes_manager.settings import Settings
from hermes_manager.system import CommandError


@pytest.fixture
def gateways(tmp_path, monkeypatch):
    """Records restarts; restarting a name in `broken` fails."""
    restarted, broken = [], set()

    def restart(p):
        if p.name in broken:
            raise CommandError(f"{p.name}: gateway not active")
        restarted.append(p.name)
    monkeypatch.setattr(people, "_gateway_up", restart)
    monkeypatch.setattr(people, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    return restarted, broken


def registry():
    return Registry({n: Person(n, 9120 + i, "lan", state="active") for i, n in enumerate(["alice", "bob", "carol"])})


def test_gateways_left_by_a_failed_restart_are_restarted_next_time(gateways):
    restarted, broken = gateways
    reg = registry()
    broken.add("bob")
    with pytest.raises(CommandError):
        people.restart_all(reg.current(), reg)
    assert restarted == ["alice"]

    broken.clear()
    people.restart_all([], reg)   # e.g. an apply that changed nothing
    assert restarted == ["alice", "bob", "carol"]
    people.restart_all([], reg)
    assert restarted == ["alice", "bob", "carol"]


def test_a_marked_restart_survives_a_failure_before_any_restart(gateways):
    restarted, _ = gateways
    reg = registry()
    people.mark_restart(["bob"])   # e.g. apply changed bob's settings, then failed
    people.restart_all([], reg)
    assert restarted == ["bob"]


def test_disabled_people_are_not_restarted(gateways):
    restarted, _ = gateways
    reg = registry()
    reg.get("bob").state = "disabled"
    people.restart_all(list(reg.people.values()), reg)
    assert restarted == ["alice", "carol"]


def test_an_oversized_env_is_refused_not_truncated(monkeypatch):
    # 1,000,001 bytes but only 500,001 characters
    monkeypatch.setattr(system, "as_user_cmd", lambda name, cmd: [sys.executable, "-c", "print('#' + 'é' * 500_000, end='')"])
    with pytest.raises(CommandError, match="larger than"):
        system.read_user_file("alice", "/home/alice/.hermes/.env")


class Incus:
    """A fake host: plays Incus and the person's container, and records every command as text."""

    def __init__(self):
        self.calls = []
        self.containers = {}            # name -> {"state", "devices", "account", "hermes", "unit"}
        self.graceful_stop_fails = False

    def run(self, cmd, *, check=True, input=None, timeout=600, env=None, cwd=None):
        self.calls.append(" ".join(cmd))
        out, code = self.answer(cmd)
        if check and code:
            raise CommandError(f"{' '.join(cmd)} -> exit {code}")
        return subprocess.CompletedProcess(cmd, code, out, "")

    def run_bytes(self, cmd, **kw):
        res = self.run(cmd, check=False)
        return res.returncode, res.stdout.encode(), b""

    def answer(self, cmd):
        if cmd[0] == "curl":
            return "302", 0
        if cmd[:2] == ["incus", "list"]:
            c = self.containers.get(cmd[-1].strip("^$"))
            return (c["state"] + "\n" if c else ""), 0
        if cmd[:2] == ["incus", "init"]:
            self.containers[cmd[3]] = {"state": "STOPPED", "devices": set(), "account": False, "hermes": False,
                                       "unit": False}
        elif cmd[:4] == ["incus", "config", "device", "list"]:
            return "\n".join(sorted(self.containers[cmd[4]]["devices"])), 0
        elif cmd[:4] == ["incus", "config", "device", "add"]:
            self.containers[cmd[4]]["devices"].add(cmd[5])
        elif cmd[:2] == ["incus", "start"]:
            self.containers[cmd[2]]["state"] = "RUNNING"
        elif cmd[:2] == ["incus", "stop"]:
            if self.graceful_stop_fails and "--force" not in cmd:
                return "", 1
            self.containers[cmd[2]]["state"] = "STOPPED"
        elif cmd[:2] == ["incus", "exec"]:
            return self.inside(self.containers[cmd[2]], cmd[cmd.index("--") + 1:])
        return "", 0

    def inside(self, c, cmd):
        if cmd[0] == "runuser":   # as the person: runuser -u NAME -- env -i VAR=... command
            cmd = list(itertools.dropwhile(lambda a: "=" in a, cmd[cmd.index("-i") + 1:]))
        if cmd[:2] == ["id", "-u"]:
            return "", 0 if c["account"] else 1
        if cmd[0] == "getent":
            return "", 0 if c.get("internet", True) else 2
        if cmd[0] == "useradd":
            c["account"] = True
        elif cmd[:2] == ["test", "-d"]:
            return "", 0 if c["hermes"] else 1
        elif cmd[:2] == ["test", "-e"]:
            return "", 0 if c["unit"] else 1
        elif cmd[:2] == ["bash", "/tmp/hermes-install.sh"]:
            c["hermes"] = True
        elif cmd[:3] == ["hermes", "gateway", "install"]:
            c["unit"] = True
        elif cmd[:3] == ["hermes", "plugins", "list"]:
            return "model-guard  enabled\n", 0
        elif cmd[:2] == ["systemctl", "--user"]:
            return "active\n", 0
        return "", 0

    def index(self, prefix):
        """Position of the first command starting with `prefix`."""
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def ran(self, text):
        return any(text in call for call in self.calls)


S = Settings(model="m1", allowed_models=("m1",), access_default="local", language_pack="hermes-lang-cs")


@pytest.fixture
def incus(tmp_path, monkeypatch):
    fake = Incus()
    for module in (system, people):
        monkeypatch.setattr(module, "run", fake.run)
    monkeypatch.setattr(system, "run_bytes", fake.run_bytes)
    monkeypatch.setattr(people, "uid_of", lambda name: None)
    monkeypatch.setattr(host, "release", lambda: ("v2", "c0ffee"))
    monkeypatch.setattr(host, "installer", lambda: "#!/bin/sh\n")
    monkeypatch.setattr(host, "reconcile_access", lambda s, reg: None)
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    monkeypatch.setattr(people, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    return fake


def test_a_new_person_gets_their_own_container_with_hermes(incus):
    reg = Registry()
    people.add(S, reg, "alice", "local")
    assert reg.get("alice").state == "active" and reg.get("alice").url == "http://alice.localhost:9120/"
    assert incus.ran("incus init images:debian/13 alice --profile hm-person")   # nothing from the default profile
    assert incus.ran("incus config device add alice eth0 nic nictype=routed ipv4.address=10.78.0.1 host_name=hm-9120")
    assert incus.ran("incus config device add alice policy disk source=/etc/hermes path=/etc/hermes readonly=true")
    assert incus.ran("incus config device add alice webapp proxy listen=tcp:127.0.0.1:9120 connect=tcp:127.0.0.1:9120")
    assert incus.ran("useradd --create-home --shell /bin/bash --uid 1000 --groups sudo alice")
    assert incus.ran("bash /tmp/hermes-install.sh --channel stable --commit c0ffee --non-interactive")
    assert incus.ran("hermes plugins install hermes-lang-cs --force --yes-deps --no-enable")
    # network before packages, packages before the account, model guard into the finished checkout
    assert incus.index("incus exec alice -- sh -c cat > /etc/systemd/network/eth0.network") \
        < incus.index("incus exec alice -- sh -c export DEBIAN_FRONTEND") < incus.index("incus exec alice -- id -u")
    assert incus.index("incus exec alice --cwd /home/alice -- runuser -u alice -- env -i HOME=/home/alice") \
        < incus.index("incus config device add alice model-guard disk") \
        < incus.index("incus exec alice --cwd /home/alice -- runuser -u alice -- env -i HOME=/home/alice "
                      "USER=alice LOGNAME=alice PATH=/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin "
                      "LANG=C.UTF-8 XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
                      "hermes gateway install")
    assert "path=/home/alice/.hermes/hermes-agent/plugins/model-guard readonly=true shift=true" in \
        incus.calls[incus.index("incus config device add alice model-guard")]
    # git ignores the read-only mount before it appears (the installer stashes untracked files)
    assert next(i for i, c in enumerate(incus.calls) if "grep -qx /plugins/model-guard/" in c) \
        < incus.index("incus config device add alice model-guard")


def test_an_interrupted_add_resumes_where_it_stopped(incus):
    reg = Registry()
    reg.add("alice", "local", (9120, 9199))
    incus.containers["alice"] = {"state": "STOPPED", "devices": {"eth0", "policy", "webapp", "model-guard"},
                                 "account": True, "hermes": True, "unit": False}
    people.add(S, reg, "alice", "local")
    assert reg.get("alice").state == "active"
    assert not any(c.startswith(("incus init", "incus config device add")) for c in incus.calls)
    assert not incus.ran("useradd")
    assert incus.ran("incus start alice")
    assert incus.ran("hermes webapp --build-only")   # the install may have stopped before it


def test_a_container_without_internet_fails_fast_with_the_fix(incus, monkeypatch):
    monkeypatch.setattr(people.time, "sleep", lambda s: None)
    incus.containers["alice"] = {"state": "RUNNING", "devices": {"eth0", "policy", "webapp"}, "internet": False}
    with pytest.raises(CommandError, match="forward chain"):
        people.ensure_packages("alice")
    assert not incus.ran("apt-get")


def test_an_unknown_container_with_the_name_is_left_alone(incus):
    incus.containers["alice"] = {"state": "RUNNING"}
    with pytest.raises(CommandError, match="container 'alice' already exists"):
        people.add(S, Registry(), "alice", "local")


def test_removing_turns_off_autostart_before_stopping_and_forces_a_stuck_container(incus):
    reg = Registry({"alice": Person("alice", 9120, "local", address="10.78.0.1", state="active")})
    incus.containers["alice"] = {"state": "RUNNING"}
    incus.graceful_stop_fails = True
    people.remove(S, reg, "alice")
    assert incus.index("incus config set alice boot.autostart=false") < incus.index("incus stop alice --timeout 60") \
        < incus.index("incus stop alice --force")
    assert reg.get("alice").state == "disabled" and incus.containers["alice"]["state"] == "STOPPED"
