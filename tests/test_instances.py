import dataclasses
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from thinny import host, instances, render, system, trace
from thinny.registry import Instance, Registry, RegistryError
from thinny.settings import Settings
from thinny.system import CommandError


@pytest.fixture
def gateways(tmp_path, monkeypatch):
    """Records restarts; restarting a name in `broken` fails."""
    restarted, broken = [], set()

    def restart(s, inst):
        if inst.name in broken:
            raise CommandError(f"{inst.name}: gateway not active")
        restarted.append(inst.name)
    monkeypatch.setattr(instances, "_gateway_up", restart)
    monkeypatch.setattr(instances, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    return restarted, broken


def registry():
    return Registry({n: Instance(n, 9120 + i, "lan", state="active") for i, n in enumerate(["alice", "bob", "carol"])})


def test_gateways_left_by_a_failed_restart_are_restarted_next_time(gateways):
    restarted, broken = gateways
    reg = registry()
    broken.add("bob")
    with pytest.raises(CommandError):
        instances.restart_all(S, reg.current(), reg)
    assert restarted == ["alice"]

    broken.clear()
    instances.restart_all(S, [], reg)   # e.g. an apply that changed nothing
    assert restarted == ["alice", "bob", "carol"]
    instances.restart_all(S, [], reg)
    assert restarted == ["alice", "bob", "carol"]


def test_a_marked_restart_survives_a_failure_before_any_restart(gateways):
    restarted, _ = gateways
    reg = registry()
    instances.mark_restart(["bob"])   # e.g. apply changed bob's settings, then failed
    instances.restart_all(S, [], reg)
    assert restarted == ["bob"]


def test_disabled_instances_are_not_restarted(gateways):
    restarted, _ = gateways
    reg = registry()
    reg.get("bob").state = "disabled"
    instances.restart_all(S, list(reg.instances.values()), reg)
    assert restarted == ["alice", "carol"]


def test_an_oversized_env_is_refused_not_truncated(monkeypatch):
    # 1,000,001 bytes but only 500,001 characters
    monkeypatch.setattr(system, "as_user_cmd", lambda name, cmd: [sys.executable, "-c", "print('#' + 'é' * 500_000, end='')"])
    with pytest.raises(CommandError, match="larger than"):
        system.read_user_file("alice", "/home/alice/.hermes/.env")


class Incus:
    """A fake host: plays Incus and the instance's container, and records every command as text."""

    def __init__(self):
        self.calls = []
        # name -> {"state", "config", "devices" (name -> properties), "account", "hermes", "unit",
        #          "files" (path -> text)}
        self.containers = {}
        self.graceful_stop_fails = False
        self.imported_mode = self.exported_mode = None   # of the last archive imported, or the directory exported to

    def run(self, cmd, *, check=True, input=None, timeout=600, env=None, cwd=None):
        self.calls.append(" ".join(cmd))
        out, code = self.answer(cmd, input)
        if check and code:
            raise CommandError(f"{' '.join(cmd)} -> exit {code}")
        return subprocess.CompletedProcess(cmd, code, out, "")

    def run_bytes(self, cmd, **kw):
        res = self.run(cmd, check=False)
        return res.returncode, res.stdout.encode(), b""

    def answer(self, cmd, input=None):
        if cmd[0] == "curl":
            return "302", 0
        if cmd[:2] == ["incus", "list"]:
            c = self.containers.get(cmd[-1].strip("^$"))
            return (c["state"] + "\n" if c else ""), 0
        if cmd[:2] == ["incus", "init"]:
            self.containers[cmd[3]] = {"state": "STOPPED", "devices": {}, "account": False, "hermes": False,
                                       "unit": False}
        elif cmd[:3] == ["incus", "config", "set"]:
            self.containers[cmd[3]].setdefault("config", {}).update(p.split("=", 1) for p in cmd[4:])
        elif cmd[:3] == ["incus", "config", "unset"]:
            if self.containers[cmd[3]].get("config", {}).pop(cmd[4], None) is None:
                return f"Error: Can't unset key '{cmd[4]}', it's not currently set", 1
        elif cmd[:2] == ["incus", "query"]:   # /1.0/instances/NAME
            return json.dumps({"devices": self.containers[cmd[2].rsplit("/", 1)[1]]["devices"]}), 0
        elif cmd[:4] == ["incus", "config", "device", "add"]:
            self.containers[cmd[4]]["devices"][cmd[5]] = {"type": cmd[6], **dict(p.split("=", 1) for p in cmd[7:])}
        elif cmd[:4] in (["incus", "config", "device", "set"], ["incus", "config", "device", "override"]):
            self.containers[cmd[4]]["devices"].setdefault(cmd[5], {}).update(p.split("=", 1) for p in cmd[6:])
        elif cmd[:2] == ["incus", "export"]:   # the fake's archives are the container as JSON
            c = self.containers[cmd[2]]
            if c.get("export_fails"):
                return "Error: no space left on device", 1
            with open(cmd[3], "w") as f:
                json.dump(c, f)
            self.exported_mode = os.stat(os.path.dirname(cmd[3])).st_mode & 0o777
        elif cmd[:2] == ["incus", "import"]:
            self.imported_mode = os.stat(cmd[2]).st_mode & 0o777
            try:
                self.containers[cmd[3]] = json.loads(Path(cmd[2]).read_text())
            except ValueError:
                return "Error: not a backup", 1
        elif cmd[:2] == ["incus", "start"]:
            self.containers[cmd[2]]["state"] = "RUNNING"
        elif cmd[:2] == ["incus", "delete"]:
            del self.containers[cmd[-1]]
        elif cmd[:2] == ["incus", "stop"]:
            if self.graceful_stop_fails and "--force" not in cmd:
                return "", 1
            self.containers[cmd[2]]["state"] = "STOPPED"
        elif cmd[:2] == ["incus", "exec"]:
            if self.containers[cmd[2]]["state"] != "RUNNING":
                return "Error: Instance is not running", 1
            return self.inside(self.containers[cmd[2]], cmd[cmd.index("--") + 1:], input)
        elif cmd[:3] == ["btrfs", "qgroup", "show"]:   # what the instance's disk quota counts
            name = cmd[-1].rsplit("/", 1)[1]
            used = self.containers[name].get("used", 2**29) // (1 if "--sync" in cmd else 4)   # or stale
            return ("Qgroupid    Referenced    Exclusive   Path \n--------    ----------    ---------   ---- \n"
                    f"0/257       {used}   {used // 2}   containers/{name}\n"), 0
        return "", 0

    def inside(self, c, cmd, input=None):
        assert cmd[:2] == system.INTERNAL   # thinny's own, so not traced
        cmd = cmd[2:]
        if cmd[0] == "runuser":   # as the instance's account: runuser -u NAME -- env -i VAR=... command
            cmd = list(itertools.dropwhile(lambda a: "=" in a, cmd[cmd.index("-i") + 1:]))
        if cmd[:2] == ["id", "-u"]:
            return "", 0 if c["account"] else 1
        if cmd[0] == "cat":
            return c.get("files", {}).get(cmd[1], ""), 0
        if cmd[:2] == ["sh", "-c"] and 'cat > "$1.tmp"' in cmd[2]:   # write_user_file
            c.setdefault("files", {})[cmd[4]] = input
        if cmd[0].endswith("/plugins/hermes-3310/install-asterisk.sh"):   # the .env it reads
            c["asterisk_read"] = c.get("files", {}).get(cmd[0].split("/plugins/")[0] + "/.env", "")
        if cmd[:3] == ["docker", "exec", "hermes-3310-asterisk"]:
            return f" sip-reg/sip:sip.odorik.cz  sip-auth  {c.get('sip', 'Registered')}  (exp. 290s)\n", 0
        if cmd[:3] == ["timeout", "10", "getent"]:
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
        elif cmd[:3] == ["hermes", "config", "set"]:
            c.setdefault("hermes_config", {})[cmd[3]] = cmd[4]
        elif cmd[:3] == ["hermes", "config", "unset"]:
            c.get("hermes_config", {}).pop(cmd[3], None)
        elif cmd[:3] == ["hermes", "config", "get"]:
            return c.get("hermes_config", {}).get(cmd[3], ""), 0
        elif cmd[:3] == ["hermes", "plugins", "list"]:
            return json.dumps([{"name": "model-guard", "status": "enabled" if c.get("guard", True) else "not enabled",
                                "version": "1.0", "source": "bundled"}]), 0
        elif cmd[:2] == ["systemctl", "--user"]:
            return "active\n", 0
        return "", 0

    def index(self, prefix):
        """Position of the first command starting with `prefix`."""
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def ran(self, text):
        return any(text in call for call in self.calls)


S = Settings(model="m1", allowed_models=("m1",), access_default="local", language_pack="hermes-lang-cs")
LOCKED = dataclasses.replace(S, model_lock=True)


@pytest.fixture
def incus(tmp_path, monkeypatch):
    fake = Incus()
    for module in (system, instances):
        monkeypatch.setattr(module, "run", fake.run)
    monkeypatch.setattr(system, "run_bytes", fake.run_bytes)
    monkeypatch.setattr(instances, "uid_of", lambda name: None)
    monkeypatch.setattr(host, "release", lambda: ("v2", "c0ffee"))
    monkeypatch.setattr(host, "installer", lambda: "#!/bin/sh\n")
    monkeypatch.setattr(host, "reconcile_access", lambda s, reg: None)
    monkeypatch.setattr(host, "POLICIES", tmp_path / "policies")
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    monkeypatch.setattr(instances, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    monkeypatch.setattr(trace, "DIR", tmp_path / "trace")
    return fake


def test_a_new_instance_gets_its_own_container_with_hermes(incus):
    reg = Registry()
    instances.add(S, reg, "alice", "local")
    assert reg.get("alice").state == "active" and reg.get("alice").url == "http://alice.localhost:9120/"
    assert reg.get("alice").built_ins == render.BUILT_INS
    assert incus.ran("incus init images:debian/13 alice --profile thinny-instance")   # nothing from the default profile
    assert incus.ran("incus config device add alice eth0 nic nictype=routed ipv4.address=10.78.0.1 host_name=thinny-9120")
    # its own policy, read-only, before the container first starts
    assert incus.ran(f"incus config device add alice policy disk source={host.POLICIES}/alice path=/etc/hermes "
                     "readonly=true shift=false")
    assert incus.index("incus config device add alice policy") < incus.index("incus start alice")
    assert (host.POLICIES / "alice/config.yaml").read_text() == render.managed_config(S)
    assert incus.ran("incus config device add alice webapp proxy listen=tcp:127.0.0.1:9120 connect=tcp:127.0.0.1:9120")
    # its trace, read-only, and without it the container still starts
    assert incus.ran(f"incus config device add alice trace disk source={trace.DIR / 'alice'} path=/var/log/thinny "
                     "readonly=true shift=false required=false")
    assert (trace.DIR / "alice").stat().st_mode & 0o777 == 0o755   # for its account too
    assert incus.ran("useradd --create-home --shell /bin/bash --uid 1000 --groups sudo alice")
    assert incus.ran("bash /tmp/hermes-install.sh --channel stable --commit c0ffee --non-interactive")
    assert incus.ran("hermes plugins install hermes-lang-cs --force --yes-deps --enable")
    # the built-in plugins and what they need: Docker for the phone plugin, Claude Code for the subscription
    packages = incus.calls[incus.index("incus exec alice -- env THINNY=1 sh -c export DEBIAN_FRONTEND")].split()
    assert {"docker.io", "docker-cli", "gettext-base"} <= set(packages)   # without recommends: no docker-cli
    assert incus.index("incus exec alice -- env THINNY=1 usermod --append --groups docker alice") \
        < incus.index("incus exec alice -- env THINNY=1 loginctl enable-linger alice")
    claude = next(i for i, c in enumerate(incus.calls) if "curl -fsSL https://claude.ai/install.sh | bash" in c)
    assert claude < next(i for i, c in enumerate(incus.calls) if c.endswith(" claude --version")) \
        < next(i for i, c in enumerate(incus.calls) if "hermes plugins install claude-subscription-directsdk " in c)
    assert incus.ran("hermes plugins install claude-subscription-directsdk --force --yes-deps --enable")
    assert incus.ran("hermes pm install --extra piper")   # the phone plugin's packages, whatever is turned on
    assert incus.ran("hermes plugins install https://github.com/melounvitek/hermes-3310 --ref ")
    # without the model lock, the policy's model is only where the instance starts
    for setting in ("model.provider openai-codex", "model.default m1", "agent.reasoning_effort high"):
        assert incus.ran(f"hermes config set {setting}")
    assert incus.ran("hermes config set tts.piper.length_scale 0.65")   # Piper's normal speed is slow
    # network before packages, packages before the account, model guard into the finished checkout
    assert incus.index("incus exec alice -- env THINNY=1 sh -c cat > /etc/systemd/network/eth0.network") \
        < incus.index("incus exec alice -- env THINNY=1 sh -c export DEBIAN_FRONTEND") \
        < incus.index("incus exec alice -- env THINNY=1 id -u")
    assert incus.index("incus exec alice --cwd /home/alice -- env THINNY=1 runuser -u alice -- env -i HOME=/home/alice") \
        < incus.index("incus config device add alice model-guard disk") \
        < incus.index("incus exec alice --cwd /home/alice -- env THINNY=1 runuser -u alice -- env -i HOME=/home/alice "
                      "USER=alice LOGNAME=alice PATH=/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin "
                      "LANG=C.UTF-8 XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
                      "hermes gateway install")
    assert "path=/home/alice/.hermes/hermes-agent/plugins/model-guard readonly=true shift=false" in \
        incus.calls[incus.index("incus config device add alice model-guard")]
    # git ignores the read-only mount before it appears (the installer stashes untracked files)
    assert next(i for i, c in enumerate(incus.calls) if "grep -qx /plugins/model-guard/" in c) \
        < incus.index("incus config device add alice model-guard")


PHONE = {"SIP_SERVER": "sip.odorik.cz", "SIP_USER": "794000", "SIP_PASSWORD": "it's \"pa$$\" #word\\", "PHONE_PIN": "246813",
         "PHONE_ALLOWED_CALLERS": "420777000111", "PHONE_HOME_NUMBER": "00420777000111",
         "VOICE_TOOLS_OPENAI_KEY": "sk-key"}


@pytest.mark.parametrize("language, czech", [("cs", True), ("en", False)])
def test_a_new_instance_can_get_a_phone_line(incus, capsys, language, czech):
    instances.add(dataclasses.replace(S, language=language), Registry(), "alice", "local", phone=PHONE)
    env = incus.containers["alice"]["files"]["/home/alice/.hermes/.env"]
    assert env.startswith(f"{instances.AUTH_PREFIX}USERNAME=alice\n")   # the Webapp sign-in stays
    # double-quoted with \ and " escaped, as both python-dotenv (Hermes) and the plugin's install script read it
    assert env.endswith('SIP_SERVER="sip.odorik.cz"\nSIP_USER="794000"\nSIP_PASSWORD="it\'s \\"pa$$\\" #word\\\\"\n'
                        'PHONE_PIN="246813"\nPHONE_ALLOWED_CALLERS="420777000111"\n'
                        'PHONE_HOME_NUMBER="00420777000111"\nVOICE_TOOLS_OPENAI_KEY="sk-key"\n')
    assert not any(secret in call for call in incus.calls for secret in (PHONE["SIP_PASSWORD"], "246813", "sk-key"))
    for setting in ("platforms.phone.enabled true", "tts.provider piper", "tts.streaming.provider piper_stream",
                    "stt.provider openai", "stt.openai.model gpt-transcribe"):
        assert incus.ran(f"hermes config set {setting}")
    # in the language of the site, else the plugin's English and Hermes's English voice
    assert incus.ran("hermes config set plugins.entries.hermes-3310.settings.phrase_pin Zadej kód a mřížku.") == czech
    assert incus.ran("hermes config set tts.piper.voice cs_CZ-jirka-medium") == czech
    # Asterisk, as alice, reads the line from .env, and the gateway starts with it
    assert incus.containers["alice"]["asterisk_read"] == env
    assert next(i for i, c in enumerate(incus.calls) if c.endswith(" runuser -u alice -- env -i HOME=/home/alice "
        "USER=alice LOGNAME=alice PATH=/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 "
        "XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
        "/home/alice/.hermes/plugins/hermes-3310/install-asterisk.sh")) \
        < next(i for i, c in enumerate(incus.calls) if c.endswith("hermes gateway install"))
    assert "did not register" not in capsys.readouterr().err


def test_a_phone_line_that_does_not_register_is_only_warned_about(incus, monkeypatch, capsys):
    monkeypatch.setattr(instances.time, "sleep", lambda s: None)
    reg = Registry()
    reg.add("alice", "local", (9120, 9199))
    incus.containers["alice"] = {"state": "STOPPED", "devices": {}, "account": True, "hermes": True,
                                 "unit": False, "sip": "Rejected"}
    instances.add(S, reg, "alice", "local", phone=PHONE)
    assert reg.get("alice").state == "active"
    assert "the phone line did not register with sip.odorik.cz" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["office.example.org", "https://office.example.org/a/b/",
                                 "https://office.example.org/?a=1"])
def test_an_address_is_a_host_or_a_host_and_one_path(incus, url):
    with pytest.raises(CommandError, match="https://office.example.org/accounting/"):
        instances.add(S, Registry(), "alice", "url", url)
    assert not incus.calls


def test_lan_access_gives_the_link_to_the_root_certificate_for_office_pcs(incus, capsys):
    instances.add(dataclasses.replace(S, lan_address="192.168.1.10"), Registry(), "alice", "lan")
    assert "http://192.168.1.10/office-root.crt" in capsys.readouterr().out


def test_a_host_has_one_instance_on_its_root_or_one_per_path(incus):
    reg = Registry()
    instances.add(S, reg, "accounting", "url", "https://office.example.org/accounting/")
    instances.add(S, reg, "lawyers", "url", "https://office.example.org/lawyers")
    for url in ("https://office.example.org/", "https://office.example.org/accounting"):
        with pytest.raises(CommandError, match="accounting already uses https://office.example.org/accounting/"):
            instances.add(S, reg, "alice", "url", url)
    instances.change_access(S, reg, "accounting", "url", "https://office.example.org/accounting")
    assert reg.get("accounting").url == "https://office.example.org/accounting"


def test_an_instance_can_get_its_own_limits_instead_of_the_sites(incus):
    reg = Registry()
    instances.add(S, reg, "alice", "local", cpu=4, memory_mb=8192, disk_gb=50)
    assert incus.ran("incus config set alice limits.cpu=4 limits.memory=8192MiB")
    assert incus.ran("incus config device override alice root size=50GiB")   # the profile's root, resized
    assert incus.index("incus config device override alice root") \
        < incus.index("incus exec alice -- env THINNY=1 sh -c export DEBIAN_FRONTEND")   # before the packages fill it

    instances.add(S, reg, "bob", "local")
    assert not any(c.startswith(("incus config set bob", "incus config device override bob")) for c in incus.calls)


def test_the_limits_of_a_running_instance_change_without_a_restart(incus):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {"eth0": {}, "policy": {}, "webapp": {}}}
    instances.limits(reg, "alice", None, 2048, 40)
    instances.limits(reg, "alice", 3, None, 60)
    assert [c for c in incus.calls if c.startswith(("incus config set", "incus config device o", "incus config device s"))] \
        == ["incus config set alice limits.memory=2048MiB", "incus config device override alice root size=40GiB",
            "incus config set alice limits.cpu=3", "incus config device set alice root size=60GiB"]
    assert incus.containers["alice"]["state"] == "RUNNING"
    with pytest.raises(CommandError, match="--cpu, --memory-mb or --disk-gb"):
        instances.limits(reg, "alice", None, None, None)


def test_a_disk_limit_too_close_to_what_the_instance_uses_is_refused(incus):
    # btrfs would set the quota, fail halfway, and leave the instance unable to write for good. Its
    # quota counts data shared with the image or a snapshot too, which Incus's usage figure leaves out.
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {"eth0": {}}, "used": int(5.7 * 2**30)}
    with pytest.raises(CommandError, match="alice uses 5.7 GiB of disk; give it at least 7 GiB"):
        instances.limits(reg, "alice", 4, None, 6)
    assert not incus.ran("incus config set") and not incus.ran("incus config device override")
    instances.limits(reg, "alice", None, None, 7)
    assert incus.ran("incus config device override alice root size=7GiB")


def test_with_the_model_lock_the_policy_sets_the_model_and_the_guard_must_be_on(incus):
    instances.add(LOCKED, Registry(), "alice", "local")
    assert not incus.ran("hermes config set model.")   # managed: Hermes refuses it

    incus.containers["alice"]["guard"] = False
    alice = Instance("alice", 9120, "local", state="active")
    with pytest.raises(CommandError, match="model-guard is not enabled"):
        instances.check_gateway(LOCKED, alice)
    instances.check_gateway(S, alice)   # without the lock, model-guard stays off
    alice.lock, alice.lock_provider = ["m1"], "openai-codex"   # locked on its own
    with pytest.raises(CommandError, match="model-guard is not enabled"):
        instances.check_gateway(S, alice)
    alice.lock = []   # unlocked on its own
    instances.check_gateway(LOCKED, alice)


def test_an_instance_signs_in_with_the_admins_terminal(incus, monkeypatch):
    started = []
    monkeypatch.setattr(instances, "as_user_interactive", lambda name, cmd: started.append((name, cmd)) or 0)
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    instances.login(reg, "alice", "claude-subscription-directsdk-experimental")
    instances.login(reg, "alice", "openai-codex")
    assert started == [("alice", ["claude", "auth", "login"]),
                       ("alice", ["hermes", "auth", "add", "openai-codex", "--no-browser"])]


def test_an_instance_uses_its_own_model_or_the_locked_one(incus):
    alice = Instance("alice", 9120, "local", state="active")
    reg = Registry({"alice": alice})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {},
                                 "hermes_config": {"model.provider": "nous", "model.default": "hermes-4"}}
    assert instances.model_of(S, reg, "alice") == ("nous", "hermes-4")
    assert instances.model_of(LOCKED, reg, "alice") == ("openai-codex", "m1")
    alice.lock = []   # unlocked on its own
    assert instances.model_of(LOCKED, reg, "alice") == ("nous", "hermes-4")
    alice.lock, alice.lock_provider = ["m2", "m3"], "openrouter"
    assert instances.model_of(S, reg, "alice") == ("openrouter", "m2")
    alice.lock = None
    incus.containers["alice"]["hermes_config"].clear()   # e.g. unset by a former lock
    assert instances.model_of(S, reg, "alice") == ("openai-codex", "m1")


def test_an_instance_locked_on_its_own_allows_only_its_models(incus, gateways, monkeypatch):
    restarted, _ = gateways
    reg = Registry({n: Instance(n, 9120 + i, "local", state="active") for i, n in enumerate(["alice", "bob"])})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {},
                                 "hermes_config": {"model.provider": "nous", "model.default": "hermes-4"}}
    saved = []
    monkeypatch.setattr(Registry, "save", lambda self, path=None: saved.append(
        (self.get("alice").lock, dict(incus.containers["alice"]["hermes_config"]))))
    instances.lock(S, reg, "alice", [])
    alice = reg.get("alice")
    assert (alice.lock, alice.lock_provider) == (["hermes-4"], "nous")   # the model it uses
    # saved while its config still has it: a failure on the way leaves the lock for apply to finish
    assert saved == [(["hermes-4"], {"model.provider": "nous", "model.default": "hermes-4"})]
    assert host.policy_current(dataclasses.replace(LOCKED, model_provider="nous", model="hermes-4",
                                                   allowed_models=("hermes-4",)), "alice")
    assert incus.containers["alice"]["hermes_config"] == {}   # pinned now, see apply
    assert incus.containers["alice"]["devices"]["policy"]["source"] == str(host.POLICIES / "alice")
    assert restarted == ["alice"]

    instances.lock(S, reg, "alice", ["m2", "m3"])
    assert (alice.lock, alice.lock_provider) == (["m2", "m3"], "nous")
    assert (host.POLICIES / "alice/allowed-models.txt").read_text().endswith("\nm2\nm3\n")
    assert restarted == ["alice", "alice"] and not (host.POLICIES / "bob").exists()


def test_an_instance_unlocked_on_its_own_starts_with_the_model_it_was_locked_to(incus, gateways):
    restarted, _ = gateways
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}}
    instances.unlock(LOCKED, reg, "alice")
    assert reg.get("alice").lock == [] and host.policy_current(S, "alice") and restarted == ["alice"]
    assert incus.containers["alice"]["hermes_config"] == \
        {"model.provider": "openai-codex", "model.default": "m1", "agent.reasoning_effort": "high"}


def test_an_interrupted_add_resumes_where_it_stopped(incus):
    reg = Registry()
    reg.add("alice", "local", (9120, 9199))
    incus.containers["alice"] = {"state": "STOPPED",
                                 "devices": {d: {} for d in ("eth0", "policy", "webapp", "trace", "model-guard")},
                                 "account": True, "hermes": True, "unit": False}
    instances.add(S, reg, "alice", "local")
    assert reg.get("alice").state == "active"
    assert not any(c.startswith(("incus init", "incus config device add")) for c in incus.calls)
    assert not incus.ran("useradd")
    assert incus.ran("incus start alice")
    assert incus.ran("hermes webapp --build-only")   # the install may have stopped before it


def test_a_container_without_internet_fails_fast_with_the_fix(incus, monkeypatch):
    monkeypatch.setattr(instances.time, "sleep", lambda s: None)
    incus.containers["alice"] = {"state": "RUNNING", "devices": {d: {} for d in ("eth0", "policy", "webapp")},
                                 "internet": False}
    with pytest.raises(CommandError, match="forward chain"):
        instances.ensure_packages("alice")
    assert not incus.ran("apt-get")


def test_an_unknown_container_with_the_name_is_left_alone(incus):
    incus.containers["alice"] = {"state": "RUNNING"}
    with pytest.raises(CommandError, match="container 'alice' already exists"):
        instances.add(S, Registry(), "alice", "local")


def test_disabling_turns_off_autostart_before_stopping_and_forces_a_stuck_container(incus):
    reg = Registry({"alice": Instance("alice", 9120, "local", address="10.78.0.1", state="active")})
    incus.containers["alice"] = {"state": "RUNNING"}
    incus.graceful_stop_fails = True
    instances.disable(S, reg, "alice")
    assert incus.index("incus config set alice boot.autostart=false") < incus.index("incus stop alice --timeout 60") \
        < incus.index("incus stop alice --force")
    assert reg.get("alice").state == "disabled" and incus.containers["alice"]["state"] == "STOPPED"


@pytest.fixture
def typed(monkeypatch):
    """The admin's answer on a terminal; None: no terminal."""
    def answer(text):
        monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"isatty": lambda self: text is not None})())
        monkeypatch.setattr("builtins.input", lambda prompt="": text)
    return answer


def test_removing_a_disabled_instance_deletes_its_container_but_keeps_its_name_and_trace(incus, typed, capsys):
    reg = Registry({"alice": Instance("alice", 9120, "local", address="10.78.0.1", state="disabled")})
    incus.containers["alice"] = {"state": "STOPPED"}
    host.write_policy(S, "alice")
    (trace.DIR / "alice").mkdir(parents=True)
    (trace.DIR / "alice/admin-access.log").write_text("2026-11-15T10:00:00+01:00 incus exec alice -- bash\n")
    typed("alice")
    instances.remove(S, reg, "alice")
    assert incus.ran("incus delete --force alice") and "alice" not in incus.containers
    assert not (host.POLICIES / "alice").exists()
    assert (trace.DIR / "alice/admin-access.log").exists()
    assert str(trace.DIR / "alice") in capsys.readouterr().out
    assert reg.get("alice").state == "removed"
    with pytest.raises(CommandError, match="alice was removed; names are not reused"):
        instances.add(S, reg, "alice", "local")
    with pytest.raises(RegistryError, match="alice is removed"):
        instances.disable(S, reg, "alice")


def test_removing_a_pending_instance_withdraws_its_access(incus, typed, monkeypatch):
    reg = Registry({"alice": Instance("alice", 9120, "lan", address="10.78.0.1", state="pending")})
    incus.containers["alice"] = {"state": "RUNNING"}   # its add stopped after the container started
    published = []
    monkeypatch.setattr(host, "reconcile_access", lambda s, reg: published.append([i.name for i in reg.current()]))
    typed("alice")
    instances.remove(S, reg, "alice")
    assert "alice" not in incus.containers and published == [[]]


def test_an_active_instance_must_be_disabled_before_it_is_removed(incus, typed):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING"}
    typed("alice")
    with pytest.raises(CommandError, match="alice is active; disable it first: thinny instance disable alice"):
        instances.remove(S, reg, "alice")
    assert "alice" in incus.containers and reg.get("alice").state == "active"


@pytest.mark.parametrize("answer", ["bob", "", None])
def test_nothing_is_removed_unless_the_admin_types_its_name(incus, typed, answer):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="disabled")})
    incus.containers["alice"] = {"state": "STOPPED"}
    typed(answer)
    with pytest.raises(CommandError, match="nothing removed"):
        instances.remove(S, reg, "alice")
    assert "alice" in incus.containers and reg.get("alice").state == "disabled"
