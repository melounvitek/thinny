import base64
import dataclasses
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from thinny import host, instances, render, settings, system, trace
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
        self.pools = {}   # name -> {"size", "used"} in bytes
        self.graceful_stop_fails = False
        self.imported_mode = self.exported_mode = None   # of the last archive imported, or the directory exported to
        # provider -> its Hermes's default model (DEFAULT_MODEL)
        self.default_models = {"openai-codex": "gpt-6-sol", instances.CLAUDE_PROVIDER: "claude-sonnet-5-5[1m]"}

    def run(self, cmd, *, check=True, input=None, timeout=600, env=None, cwd=None):
        self.calls.append(" ".join(cmd))
        out, code = self.answer(cmd, input)
        if check and code:
            raise CommandError(f"{' '.join(cmd)} -> exit {code}")
        return subprocess.CompletedProcess(cmd, code, out, "")

    def run_bytes(self, cmd, **kw):
        res = self.run(cmd, check=False)
        out = res.stdout
        return res.returncode, out if isinstance(out, bytes) else out.encode(), b""

    def answer(self, cmd, input=None):
        if cmd[0] == "curl":
            return "302", 0
        if cmd[:2] == ["incus", "list"] and "json" in cmd:   # every container, with its state
            return json.dumps([{"name": name, "status": c["state"].title(), "expanded_config": c.get("config", {}),
                                "state": c.get("incus_state", {})} for name, c in self.containers.items()]), 0
        if cmd[:2] == ["incus", "list"]:
            c = self.containers.get(cmd[-1].strip("^$"))
            return (c["state"] + "\n" if c else ""), 0
        if cmd[:3] == ["incus", "storage", "list"]:   # every pool, as JSON
            return json.dumps([{"name": name, "config": {"size": f"{p['size'] // 2**30}GiB"}}
                               for name, p in self.pools.items()]), 0
        if cmd[:2] == ["incus", "init"]:
            self.containers[cmd[3]] = {"state": "STOPPED", "devices": {}, "account": False, "hermes": False,
                                       "unit": False, "pool": cmd[cmd.index("--storage") + 1]}
        elif cmd[:3] == ["incus", "storage", "create"]:   # NAME btrfs size=NGiB ...
            self.pools[cmd[3]] = {"size": int(cmd[5].removeprefix("size=").removesuffix("GiB")) * 2**30, "used": 0}
        elif cmd[:3] == ["incus", "storage", "set"]:   # NAME size=NGiB
            self.pools[cmd[3]]["size"] = int(cmd[4].removeprefix("size=").removesuffix("GiB")) * 2**30
        elif cmd[:3] == ["incus", "storage", "delete"]:
            if any(c.get("pool") == cmd[3] for c in self.containers.values()):
                return "Error: The storage pool is currently in use", 1
            del self.pools[cmd[3]]
        elif cmd[:3] == ["incus", "config", "set"]:
            self.containers[cmd[3]].setdefault("config", {}).update(p.split("=", 1) for p in cmd[4:])
        elif cmd[:3] == ["incus", "config", "unset"]:
            if self.containers[cmd[3]].get("config", {}).pop(cmd[4], None) is None:
                return f"Error: Can't unset key '{cmd[4]}', it's not currently set", 1
        elif cmd[:2] == ["incus", "query"] and cmd[2].startswith("/1.0/storage-pools/"):   # .../NAME/resources
            p = self.pools.get(cmd[2].split("/")[3])
            if p is None:
                return "Error: Storage pool not found", 1
            return json.dumps({"inodes": {"total": 0, "used": 0}, "space": {"used": p["used"], "total": p["size"]}}), 0
        elif cmd[:2] == ["incus", "query"]:   # /1.0/instances/NAME
            return json.dumps({"devices": self.containers[cmd[2].rsplit("/", 1)[1]]["devices"]}), 0
        elif cmd[:4] == ["incus", "config", "device", "add"]:
            self.containers[cmd[4]]["devices"][cmd[5]] = {"type": cmd[6], **dict(p.split("=", 1) for p in cmd[7:])}
        elif cmd[:4] == ["incus", "config", "device", "unset"]:
            del self.containers[cmd[4]]["devices"][cmd[5]][cmd[6]]
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
                container = json.loads(Path(cmd[2]).read_text())
            except ValueError:
                return "Error: not a backup", 1
            for device, d in container["devices"].items():
                if d["type"] == "disk" and "source" in d and d.get("required") != "false" \
                        and not Path(d["source"]).exists():
                    return f'Error: Missing source path "{d["source"]}" for disk "{device}"', 1
            self.containers[cmd[3]] = container | {"pool": cmd[cmd.index("--storage") + 1]}
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
        if cmd[:2] == ["sh", "-c"] and "git fetch" in cmd[2] and c.get("github_down"):
            return "", 128
        if cmd[:2] == ["sh", "-c"] and 'cat > "$1.tmp"' in cmd[2]:   # write_user_file
            c.setdefault("files", {})[cmd[4]] = input
        if cmd[0].endswith("/plugins/hermes-3310/install-asterisk.sh"):   # the .env it reads
            c["asterisk_read"] = c.get("files", {}).get(cmd[0].split("/plugins/")[0] + "/.env", "")
        if cmd[:3] == ["docker", "exec", "hermes-3310-asterisk"]:
            return f" sip-reg/sip:sip.odorik.cz  sip-auth  {c.get('sip', 'Registered')}  (exp. 290s)\n", 0
        if cmd[:3] == ["timeout", "10", "getent"]:
            return "", 0 if c.get("internet", True) else 2
        if cmd[:2] == ["uname", "-m"]:
            return c.get("arch", "x86_64") + "\n", 0
        if cmd[:2] == [instances.HIMALAYA_BIN, "--version"]:   # its first line, as the real one prints it
            return (f"himalaya {c['himalaya']} +imap +smtp\n", 0) if c.get("himalaya") else ("", 127)
        if cmd[:2] == ["sh", "-c"] and "sha256sum" in cmd[2]:   # himalaya's release, from its URL
            c["himalaya"] = cmd[4].split("/download/")[1].split("/")[0]
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
        elif cmd[:3] == ["hermes", "config", "get"]:   # a key, or a section's keys as YAML
            config = c.get("hermes_config", {}) | c.get("pinned", {})   # with what its policy pins
            return config.get(cmd[3]) or "".join(f"{k.removeprefix(cmd[3] + '.')}: {v}\n" for k, v in config.items()
                                                 if k.startswith(cmd[3] + ".")), 0
        elif cmd[:2] == ["rm", "-f"]:
            c.get("files", {}).pop(cmd[2], None)
        elif cmd[:3] == ["hermes", "plugins", "list"]:
            return json.dumps([{"name": "model-guard", "status": "enabled" if c.get("guard", True) else "not enabled",
                                "version": "1.0", "source": "bundled"}]), 0
        elif cmd[:2] == ["systemctl", "--user"]:
            return "active\n", 0
        elif cmd[:2] == ["hermes", "usage"]:   # its JSON; none: Hermes got nothing; an error: it did not finish
            if isinstance(c.get("usage"), CommandError):
                raise c["usage"]
            return c.get("usage", ""), 0 if c.get("usage") else 1
        elif cmd[:3] == ["hermes", "sessions", "refresh-prompts"]:
            return "", c.get("refresh_exit", 0)
        elif cmd[:3] == ["hermes", "auth", "status"]:
            return f"{cmd[3]}: {'logged in' if c.get('signed_in', True) else 'logged out'}\n", 0
        elif cmd[:3] == ["claude", "auth", "status"]:   # its JSON
            return c.get("claude_status", '{"loggedIn": false}'), 0
        elif cmd[:2] == ["sh", "-c"] and instances.DEFAULT_MODEL in cmd:
            model = self.default_models.get(input)
            return (f"{model}\n", 0) if model else ("\n", 1)
        elif cmd[:2] == ["sh", "-c"] and instances.MODELS in cmd:   # provider -> model ids, its JSON or none
            models = c.get("models")
            return (models if isinstance(models, str) else json.dumps(models)), 0 if models is not None else 1
        return "", 0

    def index(self, prefix):
        """Position of the first command starting with `prefix`."""
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def ran(self, text):
        return any(text in call for call in self.calls)

    def where(self, text):
        """Position of the first command with `text` in it."""
        return next(i for i, call in enumerate(self.calls) if text in call)


S = Settings(model="m1", allowed_models=("m1",), access_default="local", language_pack="hermes-lang-cs")
LOCKED = dataclasses.replace(S, model_lock=True)
MARK = " ".join(system.INTERNAL)   # on thinny's commands in containers


@pytest.fixture
def incus(tmp_path, monkeypatch):
    fake = Incus()
    for module in (system, instances):
        monkeypatch.setattr(module, "run", fake.run)
    monkeypatch.setattr(system, "run_bytes", fake.run_bytes)
    monkeypatch.setattr(instances, "uid_of", lambda name: None)
    monkeypatch.setattr(host, "release",   # of Hermes, or of WebUI
                        lambda path=None: ("w1", "beef") if path == host.WEBUI_RELEASE_FILE else ("v2", "c0ffee"))
    monkeypatch.setattr(host, "installer", lambda: "#!/bin/sh\n")
    monkeypatch.setattr(host, "reconcile_access", lambda s, reg: None)
    monkeypatch.setattr(host, "ufw_lacks_forward", lambda: False)   # whatever this machine's ufw does
    server_disk(monkeypatch, 1000 * 2**30)
    monkeypatch.setattr(host, "POLICIES", tmp_path / "policies")
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    monkeypatch.setattr(instances, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    monkeypatch.setattr(trace, "DIR", tmp_path / "trace")
    monkeypatch.setattr(trace, "UNSENT", tmp_path / "trace-unsent")
    return fake


def test_a_new_instance_gets_its_own_container_with_hermes(incus):
    reg = Registry()
    instances.add(S, reg, "alice", "local")
    assert reg.get("alice").state == "active" and reg.get("alice").url == "http://alice.localhost:9120/"
    assert reg.get("alice").built_ins == render.BUILT_INS
    # its own pool, sized to its disk, so df in it shows its disk and not the server's; compressed (about half)
    assert incus.ran("incus storage create thinny-alice btrfs size=30GiB "
                     "btrfs.mount_options=user_subvol_rm_allowed,compress-force=zstd:1")
    # nothing from the default profile
    assert incus.ran("incus init images:debian/13 alice --profile thinny-instance --storage thinny-alice")
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
    packages = incus.calls[incus.index(f"incus exec alice -- {MARK} sh -c export DEBIAN_FRONTEND")].split()
    assert {"docker.io", "docker-cli", "gettext-base"} <= set(packages)   # without recommends: no docker-cli
    assert incus.index(f"incus exec alice -- {MARK} usermod --append --groups docker alice") \
        < incus.index(f"incus exec alice -- {MARK} loginctl enable-linger alice")
    claude = next(i for i, c in enumerate(incus.calls) if "curl -fsSL https://claude.ai/install.sh | bash" in c)
    assert claude < next(i for i, c in enumerate(incus.calls) if c.endswith(" claude --version")) \
        < next(i for i, c in enumerate(incus.calls) if "hermes plugins install claude-subscription-directsdk " in c)
    assert incus.ran("hermes plugins install claude-subscription-directsdk --force --yes-deps --enable")
    assert incus.ran("hermes pm install --extra piper")   # the phone plugin's packages, whatever is turned on
    assert incus.ran("hermes plugins install https://github.com/melounvitek/hermes-3310 --ref ")
    # without the model lock, it starts on its Hermes's default for its provider, not the server's model
    for setting in ("model.provider openai-codex", "model.default gpt-6-sol", "agent.reasoning_effort high"):
        assert incus.ran(f"hermes config set {setting}")
    assert incus.where(instances.DEFAULT_MODEL) > next(i for i, c in enumerate(incus.calls) if "hermes-install.sh" in c)
    assert incus.ran("hermes config set tts.piper.length_scale 0.65")   # Piper's normal speed is slow
    # network before packages, packages before the account, model guard into the finished checkout
    assert incus.index(f"incus exec alice -- {MARK} sh -c cat > /etc/systemd/network/eth0.network") \
        < incus.index(f"incus exec alice -- {MARK} sh -c export DEBIAN_FRONTEND") \
        < incus.index(f"incus exec alice -- {MARK} id -u")
    assert incus.index(f"incus exec alice --cwd /home/alice -- {MARK} runuser -u alice -- env -i HOME=/home/alice") \
        < incus.index("incus config device add alice model-guard disk") \
        < incus.index(f"incus exec alice --cwd /home/alice -- {MARK} runuser -u alice -- env -i HOME=/home/alice "
                      "USER=alice LOGNAME=alice PATH=/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin "
                      "LANG=C.UTF-8 XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
                      "hermes gateway install")
    assert "path=/home/alice/.hermes/hermes-agent/plugins/model-guard readonly=true shift=false" in \
        incus.calls[incus.index("incus config device add alice model-guard")]
    # git ignores the read-only mount before it appears (the installer stashes untracked files)
    assert next(i for i, c in enumerate(incus.calls) if "grep -qx /plugins/model-guard/" in c) \
        < incus.index("incus config device add alice model-guard")


@pytest.mark.parametrize("provider, model", [("openai-codex", "gpt-6-sol"),
                                             (instances.CLAUDE_PROVIDER, "claude-sonnet-5-5[1m]")])
def test_a_new_instance_starts_on_hermess_default_model_for_its_provider(incus, capsys, provider, model):
    instances.add(dataclasses.replace(S, model_provider=provider), Registry(), "alice", "local")
    config = incus.containers["alice"]["hermes_config"]
    assert (config["model.provider"], config["model.default"]) == (provider, model)
    assert f"alice starts on {model}. To pick another once it is signed in: thinny instance model alice <model>" \
        in capsys.readouterr().out.splitlines()


def test_a_new_instance_in_a_group_that_sets_the_model_starts_on_the_groups(incus, capsys):
    s = dataclasses.replace(S, groups={"sales": {"model": {"default": "gpt-5.5"}}})
    instances.add(s, Registry(), "alice", "local", group="sales")
    assert not incus.ran(instances.DEFAULT_MODEL) and not incus.ran("hermes config set model.default")
    assert "alice starts on gpt-5.5, which its group sets: thinny config edit --group sales" \
        in capsys.readouterr().out.splitlines()


def test_an_add_without_a_default_model_stops_before_the_gateway_and_resumes(incus):
    reg = Registry()
    incus.default_models = {}   # e.g. a Hermes that moved its default models elsewhere
    with pytest.raises(CommandError, match=re.escape("alice's Hermes gave no default model for openai-codex (exit 1). "
                                                     "Fix the cause, then run thinny instance add alice again")):
        instances.add(S, reg, "alice", "local")
    assert reg.get("alice").state == "pending" and not incus.ran("hermes gateway install")
    assert "model.default" not in incus.containers["alice"].get("hermes_config", {})

    incus.default_models = {"openai-codex": "gpt-6-sol"}
    instances.add(S, reg, "alice", "local")
    assert reg.get("alice").state == "active"
    assert incus.containers["alice"]["hermes_config"]["model.default"] == "gpt-6-sol"


def test_the_default_model_comes_from_the_instances_hermes(tmp_path, monkeypatch):
    """DEFAULT_MODEL with a stand-in for Hermes: its default for a provider, or the provider's first model."""
    for path, text in {
        "hermes_cli/__init__.py": "",
        "hermes_cli/models.py": """
def get_default_model_for_provider(provider):
    print("a warning on the way")
    return {"openai-codex": "gpt-6-sol"}.get(provider, "")
""",
        "providers/__init__.py": """
class Profile:
    fallback_models = ("claude-sonnet-5-5[1m]", "claude-opus-5-5[1m]")
def get_provider_profile(slug):
    return Profile() if slug == "claude-subscription-directsdk-experimental" else None
""",
    }.items():
        (tmp_path / "hermes-agent" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "hermes-agent" / path).write_text(text)
    monkeypatch.setattr(instances, "hermes_python", lambda name, script, input, **kw: subprocess.run(
        [sys.executable, "-I", "-c", script, str(tmp_path)], input=input, capture_output=True, text=True))
    assert instances.hermes_default_model("alice", "openai-codex") == "gpt-6-sol"
    assert instances.hermes_default_model("alice", instances.CLAUDE_PROVIDER) == "claude-sonnet-5-5[1m]"
    with pytest.raises(CommandError, match="alice's Hermes gave no default model for nous"):
        instances.hermes_default_model("alice", "nous")


@pytest.mark.parametrize("token, step", [("", False), ("1000:" + "M" * 35, True)])
def test_a_new_instance_ends_with_the_telegram_step_on_a_site_with_a_manager_bot(incus, capsys, token, step):
    settings.SECRETS_ENV.write_text(f"THINNY_TELEGRAM_MANAGER_TOKEN={token}\n")
    instances.add(S, Registry(), "alice", "local")
    out = capsys.readouterr().out
    assert "thinny instance login alice" in out
    assert ("Then send its user a Telegram bot: thinny instance telegram alice" in out) == step


@pytest.mark.parametrize("domain, step", [("", False), ("example.org", True)])
def test_a_new_instance_ends_with_the_mailbox_step_on_a_site_with_purelymail(incus, capsys, domain, step):
    instances.add(dataclasses.replace(S, email_domain=domain), Registry(), "alice", "local")
    out = capsys.readouterr().out
    assert ("Then give it a mailbox, alice@example.org: thinny instance email alice <its user's address>" in out) \
        == step


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
    assert incus.ran("hermes config set plugins.entries.hermes-3310.settings.phrase_approval_keys "
                     "Jedničkou to jednou povolíš, dvojkou odmítneš.") == czech
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


# Instances on one host would share the browser's origin: one's users could act as another's.
@pytest.mark.parametrize("url", ["office.example.org", "https://office.example.org/accounting/",
                                 "https://office.example.org/accounting", "https://office.example.org/?a=1"])
def test_an_address_is_a_host_of_its_own_without_a_path(incus, url):
    with pytest.raises(CommandError, match="a host of its own and no path"):
        instances.add(S, Registry(), "alice", "url", url)
    assert not incus.calls


def test_lan_access_gives_the_link_to_the_root_certificate_for_office_pcs(incus, capsys):
    instances.add(dataclasses.replace(S, lan_address="192.168.1.10"), Registry(), "alice", "lan")
    assert "http://192.168.1.10/office-root.crt" in capsys.readouterr().out


def test_a_host_has_one_instance(incus):
    reg = Registry()
    instances.add(S, reg, "accounting", "url", "https://accounting.example.org/")
    for url in ("https://accounting.example.org", "https://accounting.example.org:8443/"):
        with pytest.raises(CommandError, match="accounting already uses https://accounting.example.org/"):
            instances.add(S, reg, "alice", "url", url)
    instances.change_access(S, reg, "accounting", "url", "https://accounting.example.org")
    assert reg.get("accounting").url == "https://accounting.example.org"


def test_an_instance_can_get_its_own_limits_instead_of_the_sites(incus):
    reg = Registry()
    instances.add(S, reg, "alice", "local", cpu=4, memory_mb=8192, disk_gb=50)
    assert incus.ran("incus storage create thinny-alice btrfs size=50GiB")   # its disk from the start
    assert incus.ran("incus config set alice limits.cpu=4 limits.memory=8192MiB")
    assert not incus.ran("incus storage set") and not incus.ran("alice root")

    instances.add(S, reg, "bob", "local")
    assert incus.ran("incus storage create thinny-bob btrfs size=30GiB")
    assert not incus.ran("incus config set bob")


def test_the_limits_of_a_running_instance_change_without_a_restart(incus):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {"eth0": {}, "policy": {}, "webapp": {}},
                                 "pool": "thinny-alice"}
    incus.pools["thinny-alice"] = {"size": 30 * 2**30, "used": 0}
    instances.limits(reg, "alice", None, 2048, 40)
    instances.limits(reg, "alice", 3, None, 60)
    assert [c for c in incus.calls if c.startswith(("incus config set", "incus storage set"))] \
        == ["incus config set alice limits.memory=2048MiB", "incus storage set thinny-alice size=40GiB",
            "incus config set alice limits.cpu=3", "incus storage set thinny-alice size=60GiB"]
    assert incus.containers["alice"]["state"] == "RUNNING" and incus.pools["thinny-alice"]["size"] == 60 * 2**30
    with pytest.raises(CommandError, match="--cpu, --memory-mb or --disk-gb"):
        instances.limits(reg, "alice", None, None, None)


def test_a_disk_can_grow_but_not_shrink(incus):
    # Incus cannot shrink a pool.
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {"eth0": {}}, "pool": "thinny-alice"}
    incus.pools["thinny-alice"] = {"size": 30 * 2**30, "used": 0}
    with pytest.raises(CommandError, match="alice has 30 GiB of disk; a disk can grow but not shrink"):
        instances.limits(reg, "alice", 4, None, 20)
    assert not incus.ran("incus config set")   # nothing half done
    instances.limits(reg, "alice", None, None, 30)   # its size already
    assert not incus.ran("incus storage set")


def test_the_instances_disks_together_take_at_most_80_percent_of_the_servers(incus, monkeypatch):
    # Each pool is a file on the server's disk, which grows as its instance writes: full, they would leave the
    # server no room for itself.
    server_disk(monkeypatch, 100 * 2**30)
    incus.pools = {"thinny-bob": {"size": 30 * 2**30, "used": 0},
                   "thinny-carol": {"size": 20 * 2**30, "used": 0}}   # e.g. left by a move here that failed
    reg = Registry()
    with pytest.raises(CommandError, match=r"alice cannot have 40 GiB of disk: the instances' disks would take "
                                           r"90 GiB together, more than 80% of the server's disk \(80 GiB\)"):
        instances.add(S, reg, "alice", "local", disk_gb=40)
    assert "thinny-alice" not in incus.pools and not incus.ran("incus init")
    instances.add(S, reg, "alice", "local")   # its 30 GiB fit
    with pytest.raises(CommandError, match="alice cannot have 31 GiB of disk"):
        instances.limits(reg, "alice", None, None, 31)
    assert incus.pools["thinny-alice"]["size"] == 30 * 2**30


def server_disk(monkeypatch, total):
    """The size of the server's disk, where Incus keeps the pools' files."""
    monkeypatch.setattr(shutil, "disk_usage", lambda path: types.SimpleNamespace(
        total=total, used=0, free=total) if str(path) == "/var/lib/incus" else pytest.fail(f"disk_usage({path})"))


def test_the_list_shows_what_each_instance_uses(incus, monkeypatch, capsys, tmp_path):
    reg = registry()
    reg.instances["dave"] = Instance("dave", 9123, "lan", state="active", frontend="webui")
    incus.containers = {
        "alice": {"state": "RUNNING", "config": {"limits.cpu": "2"},
                  "incus_state": {"cpu": {"usage": 0}, "memory": {"usage": 3 * 2**30, "total": 4 * 2**30}}},
        "bob": {"state": "STOPPED", "incus_state": {}},
    }   # carol has no container
    # what df shows in each container: its own pool's use and size
    incus.pools = {f"thinny-{name}": {"size": 30 * 2**30, "used": used * 2**30}
                   for name, used in (("alice", 3), ("bob", 1), ("dave", 1))}
    clock = [0.0]
    read, expected, sampling = set(), set(), threading.Condition()

    def watched(reader):
        def wrapper(name, *args):
            result = reader(name, *args)
            with sampling:
                read.add((reader.__name__, name))
                sampling.notify_all()
            return result
        return wrapper
    monkeypatch.setattr(instances, "gateway_active", watched(instances.gateway_active))
    monkeypatch.setattr(instances, "disk_use", watched(instances.disk_use))
    monkeypatch.setattr(instances, "env_values", watched(instances.env_values))
    unreadable = {"/home/dave/.hermes/.env": b"\xff"}   # its users' .env, not text

    def sleep(seconds):
        with sampling:   # the gateways and disks of the containers there are read during the CPU sample
            assert sampling.wait_for(lambda: expected <= read, timeout=5)
        clock[0] += seconds
        incus.containers["alice"]["incus_state"]["cpu"]["usage"] += 10**9   # one of its two CPUs busy
        incus.containers["dave"] = {"state": "RUNNING", "files": unreadable,   # started meanwhile
                                    "incus_state": {"memory": {"usage": 2**29, "total": 4 * 2**30}}}
    monkeypatch.setattr(instances, "time", types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0]))
    # Of alice's 3 GiB, 2 GiB are file cache. dave has no cgroup there: Incus's usage.
    monkeypatch.setattr(instances, "CGROUPS", tmp_path / "cgroup")
    alice = tmp_path / "cgroup/lxc.payload.alice"
    alice.mkdir(parents=True)
    (alice / "memory.current").write_text(f"{3 * 2**30}\n")
    (alice / "memory.stat").write_text(f"anon {2**30 - 4096}\nactive_file {2**30}\ninactive_file {2**30}\nshmem 4096\n")
    reg.get("bob").group = reg.get("dave").group = "sales"
    reg.get("alice").mailbox = "alice@example.com"
    incus.containers["alice"]["files"] = {"/home/alice/.hermes/.env": 'SIP_SERVER="sip.odorik.cz"\nSIP_USER="123456"\n'}
    reg.get("alice").url, reg.get("dave").url = "https://192.0.2.10:9120/", "https://192.0.2.10:9123/"
    readers = ("gateway_active", "disk_use", "env_values")
    expected.update((reader, name) for reader in readers for name in ("alice", "bob"))
    instances.list_instances(reg)
    assert capsys.readouterr().out.splitlines() == [
        "NAME   STATE   GROUP  FRONTEND  GATEWAY   CPU    RAM     DISK     MAIL  PHONE  URL",
        "alice  active  -      webapp    active    1.0/2  1.0/4G  3.0/30G  yes   yes    https://192.0.2.10:9120",
        "bob    active  sales  webapp    INACTIVE  -      -       1.0/30G  -     -",
        "carol  active  -      webapp    -         -      -       -        -     -",
        "dave   active  sales  webui     active    -      0.5/4G  1.0/30G  -     ?      https://192.0.2.10:9123",
    ]
    del incus.containers["dave"]   # started again during the next list
    read.clear()
    expected -= {(reader, "alice") for reader in readers}   # not in the group
    instances.list_instances(reg, "sales")
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == ["NAME", "bob", "dave"]
    # Neither had CPU use measured, nor has a mailbox.
    assert lines[0].split() == ["NAME", "STATE", "GROUP", "FRONTEND", "GATEWAY", "RAM", "DISK", "PHONE", "URL"]


@pytest.fixture
def prague():
    """The server's local time is Prague's: UTC+1 in winter."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TZ", "Europe/Prague")
        time.tzset()
        yield
    time.tzset()


def usage_json(*windows, reason=None):
    """`hermes usage --json` with these (label, used percent, reset in UTC) windows."""
    return json.dumps({"provider": "p", "source": "s", "title": "t", "plan": None, "fetched_at": "2026-11-19T15:00:00+00:00",
                       "windows": [{"label": label, "used_percent": used, "resets_at": at, "detail": None}
                                   for label, used, at in windows],
                       "details": [], "unavailable_reason": reason})


def chatgpt_auth(*emails):
    """Hermes's auth.json, signed in to ChatGPT with an account of each email: in its tokens' claims."""
    def token(email):
        claims = json.dumps({"https://api.openai.com/profile": {"email": email, "email_verified": True}})
        return f"header.{base64.urlsafe_b64encode(claims.encode()).decode().rstrip('=')}.signature"
    return json.dumps({"credential_pool": {"openai-codex": [{"access_token": token(email)} for email in emails]}})


def test_usage_shows_each_active_instances_subscription_limits_in_the_servers_time(incus, capsys, prague):
    names = ["alice", "bob", "carol", "dave", "erin", "frank", "gina", "hank"]
    reg = Registry({n: Instance(n, 9120 + i, "lan", state="active") for i, n in enumerate(names)})
    reg.get("hank").state = "disabled"
    reg.get("bob").lock, reg.get("bob").lock_provider = ["claude-opus-5-5"], instances.CLAUDE_PROVIDER
    claude = {"model.provider": instances.CLAUDE_PROVIDER}   # chosen in its own config
    incus.containers = {name: {"state": "RUNNING"} for name in names}
    incus.containers["alice"]["usage"] = usage_json(("Session", 37.0, "2026-11-19T20:00:00+00:00"),
                                                   ("Weekly", 12.4, "2026-11-26T08:00:00+00:00"))
    incus.containers["alice"]["files"] = {"/home/alice/.hermes/auth.json": chatgpt_auth("alice@example.com")}
    incus.containers["bob"]["claude_status"] = json.dumps({"loggedIn": True, "email": "bob@example.com"})
    incus.containers["bob"]["usage"] = usage_json(("Current session", 81, "2026-11-19T17:40:00Z"),
                                                 ("Opus week", 90, "2026-11-20T06:00:00Z"),
                                                 ("Current week", 64, "2026-11-23T06:00:00Z"))
    incus.containers["carol"] |= {"hermes_config": claude, "usage": usage_json(
        reason="no Claude subscription login found (run `claude` and log in)")}
    incus.containers["dave"]["signed_in"] = False   # Hermes got nothing: no credential
    # erin: Hermes got nothing, but she is signed in
    incus.containers["erin"]["files"] = {"/home/erin/.hermes/auth.json": chatgpt_auth("erin@example.com")}
    incus.containers["frank"] |= {"hermes_config": claude, "usage": usage_json(reason="Claude Code did not answer in time"),
                                  "claude_status": json.dumps({"loggedIn": True, "email": "frank@example.com"})}
    incus.containers["gina"]["usage"] = CommandError("hermes usage: timed out after 20 s")
    instances.usage(S, reg)
    assert capsys.readouterr().out.splitlines() == [
        "NAME   PROVIDER  ACCOUNT            5H           WEEK",
        "alice  ChatGPT   alice@example.com  37% → 21:00  12% → Thu 09:00",
        "bob    Claude    bob@example.com    81% → 18:40  64% → Mon 07:00",
        "carol  Claude    -                  not signed in",
        "dave   ChatGPT   -                  not signed in",
        "erin   ChatGPT   erin@example.com   ?",
        "frank  Claude    frank@example.com  Claude Code did not answer in time",
        "gina   ChatGPT   -                  ?",
    ]
    assert incus.ran(f"hermes usage --json --provider {instances.CLAUDE_PROVIDER}")
    assert not incus.ran("exec hank")
    assert not incus.ran("hermes config get model.default")   # only the provider counts

    instances.usage(S, reg, "alice")
    assert capsys.readouterr().out.splitlines()[1:] == ["alice  ChatGPT   alice@example.com  37% → 21:00  12% → Thu 09:00"]
    for name in ("hank", "nobody"):
        with pytest.raises(RegistryError):
            instances.usage(S, reg, name)


def test_usage_takes_what_an_instances_users_make_hermes_say_as_text_or_a_failure(incus, capsys, prague):
    names = ["alice", "bob", "carol", "dave"]
    reg = Registry({n: Instance(n, 9120 + i, "lan", state="active") for i, n in enumerate(names)})
    nested = "[" * 100_000   # deeper than Python's JSON parser goes
    incus.containers = {
        "alice": {"state": "RUNNING", "hermes_config": {"model.provider": "own\x1b[2J\nINJECTED"},
                  "usage": usage_json(reason="Claude Code did not answer in time\x1b[2J")},
        "bob": {"state": "RUNNING", "usage": usage_json(("Session", 37, "9999-12-31T23:59:59+00:00")),   # 10000 here
                "files": {"/home/bob/.hermes/auth.json": nested}},
        "carol": {"state": "RUNNING", "usage": usage_json(("Session", 37, "2026-11-19T20:00:00+00:00")),
                  "files": {"/home/carol/.hermes/auth.json": chatgpt_auth("carol\x1b[2J@example.com")}},
        "dave": {"state": "RUNNING", "usage": nested,   # two accounts, one signed in twice
                 "files": {"/home/dave/.hermes/auth.json": chatgpt_auth("d@example.com", "d@example.org", "d@example.com")}},
    }
    instances.usage(S, reg)
    assert capsys.readouterr().out.splitlines()[1:] == [
        "alice  own[2JINJECTED  -                             Claude Code did not answer in time[2J",   # without control characters
        "bob    ChatGPT         ?                             ?",
        "carol  ChatGPT         carol[2J@example.com          37% → 21:00  -",
        "dave   ChatGPT         d@example.com, d@example.org  ?",
    ]


def test_with_the_model_lock_the_policy_sets_the_model_and_the_guard_must_be_on(incus):
    instances.add(LOCKED, Registry(), "alice", "local")
    assert not incus.ran("hermes config set model.")   # managed: Hermes refuses it
    assert not incus.ran(instances.DEFAULT_MODEL)

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
    assert instances.model_of(S, reg, "alice") == ("openai-codex", "")   # not the server's model


def test_an_instance_is_locked_to_the_model_its_users_set_without_control_characters(incus, gateways, capsys):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {},
                                 "hermes_config": {"model.provider": "nous\x1b[2J", "model.default": "hermes-4\u202e"}}
    instances.lock(S, reg, "alice", [])
    assert capsys.readouterr().out == "alice is locked to nous[2J: hermes-4\n"
    assert (reg.get("alice").lock, reg.get("alice").lock_provider) == (["hermes-4"], "nous[2J")


def test_an_instance_without_a_model_of_its_own_is_told_to_pick_one(incus, gateways, capsys):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "models": {"openai-codex": ["gpt-6-sol"]}}
    instances.default_model(S, reg, "alice")
    assert capsys.readouterr().out.splitlines()[0] == \
        "alice: no model is set for new chats; pick one: thinny instance model alice <model>"
    with pytest.raises(CommandError, match=re.escape("alice has no model of its own: name the models: "
                                                     "thinny instance lock alice <model> ...")):
        instances.lock(S, reg, "alice", [])
    assert reg.get("alice").lock is None


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


@pytest.mark.parametrize("lock, provider, model", [(None, "openai-codex", "m1"), (["m2", "m3"], "nous", "m2")])
def test_an_instance_unlocked_on_its_own_starts_with_the_model_it_was_locked_to(incus, gateways, lock, provider,
                                                                                 model):
    restarted, _ = gateways
    alice = Instance("alice", 9120, "local", state="active", lock=lock, lock_provider="nous" if lock else "")
    reg = Registry({"alice": alice})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}}
    instances.update_policy(LOCKED, alice)   # the server's lock, or its own
    instances.unlock(LOCKED, reg, "alice")
    assert alice.lock == [] and host.policy_current(S, "alice") and restarted == ["alice"]
    assert incus.containers["alice"]["hermes_config"] == \
        {"model.provider": provider, "model.default": model, "agent.reasoning_effort": "high"}


def test_an_instance_unlocked_in_a_group_that_sets_the_model_gets_the_locks_provider_and_effort(incus, gateways):
    s = dataclasses.replace(LOCKED, groups={"sales": {"model": {"default": "m9"}}})
    alice = Instance("alice", 9120, "local", state="active", group="sales")
    reg = Registry({"alice": alice})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "pinned": {"model.default": "m9"}}
    instances.update_policy(s, alice)
    instances.unlock(s, reg, "alice")
    assert incus.containers["alice"]["hermes_config"] == \
        {"model.provider": "openai-codex", "agent.reasoning_effort": "high"}   # Hermes refuses the pinned model


CODEX = ["gpt-6-sol", "gpt-6-sol-900k", "gpt-6-luna", "gpt-6-luna-900k", "gpt-5.6-sol", "gpt-5.6-sol-900k",
         "gpt-5.6-terra", "gpt-5.6-terra-900k", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini", "gpt-5.4"]
CLAUDE = ["claude-sonnet-5-5[1m]", "claude-opus-5-5[1m]", "claude-haiku-4-5-20251001"]


def unlocked_alice(incus, models):
    """alice on ChatGPT's gpt-5.5, and Hermes offering her `models`."""
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "models": models, "hermes_config": {
        "model.provider": "openai-codex", "model.default": "gpt-5.5", "agent.reasoning_effort": "high"}}
    return Registry({"alice": Instance("alice", 9120, "local", state="active")})


def test_an_instance_shows_the_model_new_chats_start_on_and_the_models_it_can_use(incus, capsys):
    reg = unlocked_alice(incus, {"openai-codex": CODEX, "nous": ["hermes-4\x1b[2J"]})   # set up in Hermes
    instances.default_model(S, reg, "alice")
    assert capsys.readouterr().out.splitlines() == [
        "alice: new chats start on gpt-5.5 (ChatGPT)",
        "ChatGPT  gpt-6-sol, gpt-6-sol-900k, gpt-6-luna, gpt-6-luna-900k, gpt-5.6-sol, gpt-5.6-sol-900k,",
        "         gpt-5.6-terra, gpt-5.6-terra-900k, gpt-5.6-luna, gpt-5.5, gpt-5.4-mini, gpt-5.4",
        "nous     hermes-4[2J",   # without control characters, for the admin's terminal
        "Claude   not signed in (thinny instance login alice)",
    ]
    assert incus.containers["alice"]["hermes_config"]["model.default"] == "gpt-5.5"


def test_an_instance_gets_a_new_default_model_at_once_without_a_restart(incus, gateways, capsys):
    restarted, _ = gateways
    reg = unlocked_alice(incus, {"openai-codex": CODEX, instances.CLAUDE_PROVIDER: CLAUDE})
    config = incus.containers["alice"]["hermes_config"]
    instances.default_model(S, reg, "alice", "gpt-6-sol")
    assert config == {"model.provider": "openai-codex", "model.default": "gpt-6-sol", "agent.reasoning_effort": "high"}
    # Hermes takes Claude's long-context models without [1m] too, as thinny suggests them
    instances.default_model(S, reg, "alice", "claude-opus-5-5", instances.CLAUDE_PROVIDER)
    assert config == {"model.provider": instances.CLAUDE_PROVIDER, "model.default": "claude-opus-5-5",
                      "agent.reasoning_effort": "high"}
    instances.default_model(S, reg, "alice", "claude-haiku-4-5-20251001")   # of the provider it uses now
    assert config["model.provider"] == instances.CLAUDE_PROVIDER
    assert config["model.default"] == "claude-haiku-4-5-20251001"
    assert capsys.readouterr().out.splitlines() == ["alice: new chats start on gpt-6-sol (ChatGPT)",
                                                    "alice: new chats start on claude-opus-5-5 (Claude)",
                                                    "alice: new chats start on claude-haiku-4-5-20251001 (Claude)"]
    assert restarted == []   # Hermes reads it for every new chat


@pytest.mark.parametrize("model, provider, error", [
    ("gpt-9", None, "ChatGPT offers alice no model gpt-9; it offers gpt-6-sol, gpt-6-sol-900k, "),
    ("claude-opus-5-5", None, "ChatGPT offers alice no model claude-opus-5-5"),   # the provider it uses
    ("claude-opus-5-5", instances.CLAUDE_PROVIDER, "alice is not signed in to Claude: thinny instance login alice"),
])
def test_a_new_default_model_must_be_one_hermes_offers_the_instance(incus, model, provider, error):
    reg = unlocked_alice(incus, {"openai-codex": CODEX})
    with pytest.raises(CommandError, match=re.escape(error)):
        instances.default_model(S, reg, "alice", model, provider)
    assert not incus.ran("hermes config set")


def test_a_group_that_sets_the_model_keeps_it(incus):
    reg = unlocked_alice(incus, {"openai-codex": CODEX, instances.CLAUDE_PROVIDER: CLAUDE})
    reg.get("alice").group = "sales"
    s = dataclasses.replace(S, groups={"sales": {"model": {"default": "gpt-5.5"}}})
    with pytest.raises(CommandError, match=re.escape("alice's group sales decides its model.default: "
                                                     "thinny config edit --group sales")):
        instances.default_model(s, reg, "alice", "claude-opus-5-5", instances.CLAUDE_PROVIDER)
    assert not incus.ran("hermes config set")


def test_only_providers_the_instance_is_signed_in_to_list_their_models(tmp_path):
    """MODELS with a stand-in for Hermes: what its model picker offers, and whether each provider is signed in."""
    for path, text in {
        "hermes_cli/__init__.py": "",
        "hermes_cli/env_loader.py": "import os\ndef load_hermes_dotenv():\n    os.environ['OPENROUTER_API_KEY'] = 'k'\n",
        "hermes_cli/inventory.py": """
import os
def load_picker_context():
    return "ctx"
def build_models_payload(ctx, **kw):
    return {"providers": [
        {"slug": "openai-codex", "models": ["gpt-5.5"]},
        {"slug": "nous", "models": ["hermes-4"], "authenticated": False},   # in use, its sign-in gone
        {"slug": "claude-out", "models": ["opus"]},   # its program says it is signed out
        {"slug": "claude-in", "models": ["sonnet"]},
    ] + ([{"slug": "openrouter", "models": ["x"]}] if os.environ.get("OPENROUTER_API_KEY") else [])}   # from .env
""",
        "providers/__init__.py": """
class Profile:
    def __init__(self, status):
        self.status = status
    def setup_status(self):
        return self.status
def get_provider_profile(slug):
    return {"openai-codex": Profile(None), "claude-out": Profile({"logged_in": False}),
            "claude-in": Profile({"logged_in": True})}.get(slug)
""",
    }.items():
        (tmp_path / "hermes-agent" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "hermes-agent" / path).write_text(text)
    out = subprocess.run([sys.executable, "-I", "-c", instances.MODELS, str(tmp_path)], capture_output=True, text=True,
                         check=True).stdout
    assert json.loads(out) == {"openai-codex": ["gpt-5.5"], "claude-in": ["sonnet"], "openrouter": ["x"]}


@pytest.mark.parametrize("models", [None, "Traceback (most recent call last):", '["gpt-5.5"]', '{"openai-codex": 1}',
                                    '{"openai-codex": "gpt-5.5"}', '{"openai-codex": {"gpt-9": true}}'])
def test_without_the_list_of_models_nothing_is_set(incus, capsys, models):
    reg = unlocked_alice(incus, models)   # Hermes failed, or its users made it say something else
    for model in (None, "gpt-5.5"):
        with pytest.raises(CommandError, match="could not get the models alice can use from Hermes"):
            instances.default_model(S, reg, "alice", model)
    assert not incus.ran("hermes config set") and capsys.readouterr().out == ""


def test_a_locked_instance_starts_new_chats_on_its_locks_model(incus, capsys):
    reg = unlocked_alice(incus, {"openai-codex": CODEX, instances.CLAUDE_PROVIDER: CLAUDE})
    alice = reg.get("alice")
    alice.lock, alice.lock_provider = ["claude-opus-5-5", "claude-sonnet-5-5"], instances.CLAUDE_PROVIDER
    instances.default_model(S, reg, "alice")
    alice.lock = None   # the server's lock
    instances.default_model(LOCKED, reg, "alice")
    assert capsys.readouterr().out.splitlines() == [
        "alice: new chats start on claude-opus-5-5 (Claude); its lock allows claude-opus-5-5, claude-sonnet-5-5",
        "alice: new chats start on m1 (ChatGPT); its lock allows m1",
    ]
    for s, lock in ((S, ["claude-opus-5-5"]), (LOCKED, None)):
        alice.lock = lock
        with pytest.raises(CommandError, match=re.escape("alice is locked, and its lock decides its default: "
                                                         "thinny instance lock alice gpt-5.5 ...")):
            instances.default_model(s, reg, "alice", "gpt-5.5")
    assert not incus.ran("hermes config set")

    alice.lock = []   # unlocked on its own, whatever the server's lock
    instances.default_model(LOCKED, reg, "alice", "gpt-5.4")
    assert incus.containers["alice"]["hermes_config"]["model.default"] == "gpt-5.4"



SALES = {"display": {"language": "cs"}, "tts": {"provider": "edge"}}
GROUPS = dataclasses.replace(S, groups={"sales": SALES, "support": {"display": {"skin": "mono"}}})


def test_a_new_instance_in_a_group_starts_under_its_settings(incus):
    reg = Registry()
    instances.add(GROUPS, reg, "alice", "local", phone=PHONE, group="sales")
    assert reg.get("alice").group == "sales"
    assert (host.POLICIES / "alice/config.yaml").read_text() == \
        render.managed_config(dataclasses.replace(GROUPS, hermes=SALES))
    # Hermes refuses to set what the policy pins
    assert not incus.ran("hermes config set display.language") and not incus.ran("hermes config set tts.provider")
    assert incus.ran("hermes config set tts.piper.length_scale 0.65") and incus.ran("hermes config set stt.provider")

    reg.get("alice").state = "pending"   # resumed: it keeps its group
    instances.add(GROUPS, reg, "alice", "local")
    assert reg.get("alice").group == "sales"


def test_an_instance_joins_switches_and_leaves_a_group(incus, gateways, capsys):
    restarted, _ = gateways
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {},
                                 "hermes_config": {"display.language": "en", "display.skin": "default"}}
    instances.set_group(GROUPS, reg, "alice", "sales")
    assert reg.get("alice").group == "sales" and restarted == ["alice"]
    assert host.policy_current(dataclasses.replace(GROUPS, hermes=SALES), "alice")
    assert incus.containers["alice"]["hermes_config"] == {"display.skin": "default"}   # pinned now

    instances.set_group(GROUPS, reg, "alice", "support")
    assert host.policy_current(dataclasses.replace(GROUPS, hermes={"display": {"skin": "mono"}}), "alice")
    instances.set_group(GROUPS, reg, "alice", "")
    assert reg.get("alice").group == "" and host.policy_current(GROUPS, "alice")
    assert restarted == ["alice"] * 3
    assert capsys.readouterr().out.splitlines() == ["alice is in the group sales", "alice is in the group support",
                                                    "alice is in no group"]


def test_leaving_a_group_that_sets_the_model_does_not_keep_it(incus, gateways):
    # Only a lock's model stays: Hermes's defaults apply again, as with the group's other settings.
    sales = {"model": {"default": "m9"}, "mcp_servers": {"github: work": {"command": "npx"}}}
    s = dataclasses.replace(S, groups={"sales": sales})
    alice = Instance("alice", 9120, "local", state="active", group="sales")
    reg = Registry({"alice": alice})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "hermes_config": {"model.provider": "nous"}}
    instances.update_policy(s, alice)
    instances.set_group(s, reg, "alice", "")
    assert host.policy_current(s, "alice")
    assert incus.containers["alice"]["hermes_config"] == {"model.provider": "nous"}


def test_groups_are_listed_with_their_members(capsys):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active", group="sales"),
                    "bob": Instance("bob", 9121, "local", state="pending", group="sales"),
                    "carol": Instance("carol", 9122, "local", state="disabled", group="sales"),
                    "dave": Instance("dave", 9123, "local", state="active", group="research"),
                    "erin": Instance("erin", 9124, "local", state="active")})
    instances.list_groups(GROUPS, reg)
    assert capsys.readouterr().out.splitlines() == [
        "GROUP     INSTANCES",
        "research  dave   (its file is missing)",
        "sales     alice, bob",
        "support   -",
    ]

WEBUI_UNIT = "/home/alice/.config/systemd/user/hermes-webui.service"


def test_a_new_instance_can_open_webui_instead_of_the_webapp(incus, capsys):
    reg = Registry()
    instances.add(dataclasses.replace(S, language="cs"), reg, "alice", "local", frontend="webui")
    alice = incus.containers["alice"]
    assert reg.get("alice").frontend == "webui"
    # at the WebUI release the instances that use it run
    assert incus.ran("/home/alice/hermes-webui https://github.com/nesquena/hermes-webui beef")
    # on the instance's port in place of the Webapp, which stays off
    assert alice["hermes_config"]["webapp.enabled"] == "false"
    assert alice["files"][WEBUI_UNIT] == render.webui_unit("alice", 9120, secure=False)   # http://alice.localhost
    assert incus.ran("systemctl --user enable hermes-webui")
    # it starts in the site's language and theme, and a simpler look
    settings = json.loads(alice["files"]["/home/alice/.hermes/webui/settings.json"])
    assert (settings["language"], settings["theme"], settings["skin"]) == ("cs", "light", "graphite")
    assert settings["hidden_tabs"] == ["kanban", "tasks", "skills", "memory", "workspaces", "profiles", "insights", "logs"]
    assert settings["hide_composer_context"] and settings["hide_composer_profile"] and settings["hide_composer_workspace"]
    assert settings["check_for_updates"] is False
    # its users' chats on Telegram, email and the like in the sidebar, without Claude Code's
    assert settings["show_cli_sessions"] and settings["show_claude_code_sessions"] is False
    # the gateway first (scheduled jobs, the phone), then WebUI on the port the gateway leaves it
    assert incus.where("hermes gateway install") < incus.where("systemctl --user restart hermes-webui")
    out = capsys.readouterr().out
    assert "  WebUI:    http://alice.localhost:9120/\n" in out and "Username" not in out


def test_an_instance_switches_to_webui_and_back_on_its_port(incus):
    reg = Registry({"alice": Instance("alice", 9120, "local", url="http://alice.localhost:9120/", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "unit": True, "files": {
        "/home/alice/.hermes/webui/settings.json": '{"theme": "dark"}'}}   # its users' WebUI of before
    instances.change_frontend(S, reg, "alice", "webui")
    alice = incus.containers["alice"]
    assert reg.get("alice").frontend == "webui" and alice["hermes_config"]["webapp.enabled"] == "false"
    assert incus.ran("https://github.com/nesquena/hermes-webui beef")
    assert incus.where("hermes gateway restart --yes") < incus.where("systemctl --user restart hermes-webui")
    assert alice["files"]["/home/alice/.hermes/webui/settings.json"] == '{"theme": "dark"}'   # their look stays

    incus.calls.clear()
    instances.change_frontend(S, reg, "alice", "webapp")
    assert reg.get("alice").frontend == "webapp" and alice["hermes_config"]["webapp.enabled"] == "true"
    # WebUI leaves the port before the Webapp takes it, and does not start with the container any more
    assert incus.where("systemctl --user disable --now hermes-webui") < incus.where("hermes gateway restart --yes")
    assert WEBUI_UNIT not in alice["files"]
    assert not incus.ran("restart hermes-webui")


def test_chats_going_on_rebuild_their_prompts_after_a_restart(incus):
    alice = Instance("alice", 9120, "local", state="active")
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "unit": True}
    instances.start_or_restart_gateway(S, alice)
    assert incus.where("hermes gateway restart --yes") < incus.where("hermes sessions refresh-prompts")


def test_a_failed_refresh_of_the_chats_prompts_leaves_the_restart_done(incus, capsys):
    alice = Instance("alice", 9120, "local", state="active")
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "unit": True, "refresh_exit": 1}
    instances.mark_restart(["alice"])
    instances.start_or_restart_gateway(S, alice)
    assert not instances.RESTART_PENDING.exists()
    assert "alice: chats going on keep their prompts: " in capsys.readouterr().err


def test_a_switch_to_webui_that_cannot_fetch_it_leaves_the_webapp_on(incus):
    reg = Registry({"alice": Instance("alice", 9120, "local", url="http://alice.localhost:9120/", state="active")})
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "unit": True, "github_down": True}
    with pytest.raises(CommandError, match="git fetch .* -> exit 128"):
        instances.change_frontend(S, reg, "alice", "webui")
    assert reg.get("alice").frontend == "webapp"   # so config apply keeps the Webapp too
    assert not incus.ran("webapp.enabled") and not incus.ran("hermes-webui.env")


@pytest.mark.parametrize("frontend", ["webapp", "webui"])
def test_applying_the_settings_again_keeps_the_frontend_and_changes_nothing(incus, frontend):
    alice = Instance("alice", 9120, "lan", url="https://192.168.1.10:9120/", state="active", frontend=frontend)
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}}
    assert instances.configure_webapp(S, alice) is True
    assert incus.containers["alice"]["hermes_config"]["webapp.enabled"] == str(frontend == "webapp").lower()
    instances._set_pending([])
    assert instances.configure_webapp(S, alice) is False
    assert not instances._pending()
    if frontend == "webui":   # over HTTPS: Secure cookies
        assert incus.containers["alice"]["files"][WEBUI_UNIT] == render.webui_unit("alice", 9120, secure=True)
    else:   # nothing to stop
        assert not any("systemctl" in call and "hermes-webui" in call for call in incus.calls)


def signs_in(c, password):
    """Whether the Webapp and WebUI of alice's container take `password`."""
    stored = re.search(rf"^{instances.AUTH_PREFIX}PASSWORD_HASH='(.*)'$", c["files"]["/home/alice/.hermes/.env"], re.M)
    _, n, r, p, salt, key = stored[1].split("$")
    webapp = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32,
                            maxmem=0) == base64.b64decode(key)
    # as WebUI gets it: systemd reads it in double quotes, a backslash taking the next character as it is, then
    # WebUI strips it
    webui = re.fullmatch(r'HERMES_WEBUI_PASSWORD="(.*)"\n', c["files"].get("/home/alice/.config/hermes-webui.env", ""),
                         re.S)
    return webapp, bool(webui) and re.sub(r"\\(.)", r"\1", webui[1], flags=re.S).strip() == password


def printed_password(capsys):
    return re.search(r"(?i)password: (\S+)", capsys.readouterr().out)[1]


def test_one_password_signs_in_to_either_frontend_and_a_reset_changes_both(incus, capsys):
    reg = Registry()
    instances.add(S, reg, "alice", "local")
    alice, first = incus.containers["alice"], printed_password(capsys)
    assert signs_in(alice, first) == (True, True)
    instances.change_frontend(S, reg, "alice", "webui")
    assert "Password" not in capsys.readouterr().out and signs_in(alice, first) == (True, True)

    incus.calls.clear()
    instances.password(S, reg, "alice")
    second = printed_password(capsys)
    assert signs_in(alice, second) == (True, True) and signs_in(alice, first) == (False, False)
    # everyone signed in to WebUI signs in again: it keeps its sessions in memory, so it stops first
    assert incus.where("systemctl --user stop hermes-webui") < incus.where("rm -f /home/alice/.hermes/webui/.sessions.json") \
        < incus.where("systemctl --user restart hermes-webui")


def test_a_password_typed_in_signs_in_to_either_frontend_as_typed(incus, capsys):
    reg = Registry()
    instances.add(S, reg, "alice", "local")
    first = printed_password(capsys)
    typed = 'a "b\\" $HOME #c'
    instances.password(S, reg, "alice", lambda name: typed)
    assert typed not in capsys.readouterr().out
    assert signs_in(incus.containers["alice"], typed) == (True, True)
    assert signs_in(incus.containers["alice"], first) == (False, False)


def test_an_instance_from_before_webui_gets_a_new_password_when_it_switches(incus, capsys):
    # the Webapp's password is kept as a hash only, and WebUI takes none
    reg = Registry({"alice": Instance("alice", 9120, "local", url="http://alice.localhost:9120/", state="active")})
    old = {"/home/alice/.hermes/.env": f"{instances.AUTH_PREFIX}USERNAME=alice\n"
                                       f"{instances.AUTH_PREFIX}PASSWORD_HASH='{instances.password_hash('old')}'\n"}
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "unit": True, "files": old}
    instances.change_frontend(S, reg, "alice", "webui")
    new = printed_password(capsys)
    assert signs_in(incus.containers["alice"], new) == (True, True)
    instances.change_frontend(S, reg, "alice", "webapp")
    assert "Password" not in capsys.readouterr().out


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


def test_an_add_that_stopped_before_its_container_keeps_the_pool_it_has(incus):
    reg = Registry()
    reg.add("alice", "local", (9120, 9199))
    incus.pools["thinny-alice"] = {"size": 50 * 2**30, "used": 0}
    instances.add(S, reg, "alice", "local")
    assert not incus.ran("incus storage create") and incus.pools["thinny-alice"]["size"] == 50 * 2**30
    assert incus.ran("incus init images:debian/13 alice --profile thinny-instance --storage thinny-alice")


def test_a_new_instance_does_not_take_a_pool_that_exists(incus):
    incus.pools["thinny-alice"] = {"size": 30 * 2**30, "used": 0}   # e.g. a move here that failed halfway
    with pytest.raises(CommandError, match="an Incus storage pool 'thinny-alice' already exists; choose another name"):
        instances.add(S, Registry(), "alice", "local")
    assert not incus.ran("incus init")


def test_a_container_without_internet_fails_fast_with_the_fix(incus, monkeypatch):
    monkeypatch.setattr(instances.time, "sleep", lambda s: None)
    incus.containers["alice"] = {"state": "RUNNING", "devices": {d: {} for d in ("eth0", "policy", "webapp")},
                                 "internet": False}
    with pytest.raises(CommandError, match="forward chain"):
        instances.ensure_packages("alice", "thinny instance add alice")
    assert not incus.ran("apt-get")


@pytest.mark.parametrize("sudo_user, command", [(None, "ufw"), ("admin", "sudo ufw")])
def test_an_add_that_ufw_blocks_gives_the_ufw_rule_and_how_to_resume(incus, monkeypatch, sudo_user, command):
    monkeypatch.setattr(instances.time, "sleep", lambda s: None)
    monkeypatch.setattr(host, "ufw_lacks_forward", lambda: True)
    monkeypatch.delenv("SUDO_USER", raising=False)
    if sudo_user:
        monkeypatch.setenv("SUDO_USER", sudo_user)
    reg = Registry()
    reg.add("alice", "local", (9120, 9199))
    incus.containers["alice"] = {"state": "RUNNING", "devices": {d: {} for d in ("eth0", "policy", "webapp")},
                                 "internet": False}
    with pytest.raises(CommandError) as e:
        instances.add(S, reg, "alice", "local")
    assert str(e.value) == ("ufw blocks the containers' traffic, so alice's container cannot reach the internet "
                            f"(see README, Prerequisites). Run: {command} route allow in on thinny-+\n"
                            "Then run thinny instance add alice again to resume.")
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
    incus.containers["alice"] = {"state": "STOPPED", "pool": "thinny-alice"}
    incus.pools = {"thinny-alice": {"size": 30 * 2**30, "used": 0}, "thinny-bob": {"size": 30 * 2**30, "used": 0}}
    host.write_policy(S, "alice")
    (trace.DIR / "alice").mkdir(parents=True)
    (trace.DIR / "alice/admin-access.log").write_text("2026-11-15T10:00:00+01:00 incus exec alice -- bash\n")
    typed("alice")
    instances.remove(S, reg, "alice")
    assert incus.ran("incus delete --force alice") and "alice" not in incus.containers
    assert list(incus.pools) == ["thinny-bob"]   # its pool, with its disk, goes too
    assert not (host.POLICIES / "alice").exists()
    assert (trace.DIR / "alice/admin-access.log").exists()
    assert str(trace.DIR / "alice") in capsys.readouterr().out
    assert reg.get("alice").state == "removed"
    with pytest.raises(CommandError, match="alice was removed; to reuse its name: thinny instance remove alice --purge"):
        instances.add(S, reg, "alice", "local")
    with pytest.raises(RegistryError, match="alice is removed"):
        instances.disable(S, reg, "alice")


def test_purging_an_instance_frees_its_name_and_port_and_keeps_its_trace_renamed(incus, typed, capsys):
    reg = Registry({"alice": Instance("alice", 9120, "local", address="10.78.0.1", state="disabled")})
    incus.containers["alice"] = {"state": "STOPPED"}
    host.write_policy(S, "alice")
    (trace.DIR / "alice").mkdir(parents=True)
    (trace.DIR / "alice/admin-access.log").write_text("2026-11-15T10:00:00+01:00 incus exec alice -- bash\n")
    (trace.UNSENT / "alice").mkdir(parents=True)
    (trace.UNSENT / "alice/000000000001.json").write_text("{}")
    typed("alice")
    instances.remove(S, reg, "alice", purge=True)
    assert "alice" not in incus.containers and not (host.POLICIES / "alice").exists()
    [kept] = trace.DIR.glob("alice.purged-*")
    assert (kept / "admin-access.log").read_text().endswith("incus exec alice -- bash\n")
    assert str(kept) in capsys.readouterr().out
    assert not (trace.UNSENT / "alice").exists()   # not for the sheet of a new alice
    assert "alice" not in reg.instances
    new = reg.add("alice", "local", (9120, 9199))
    assert (new.port, new.address) == (9120, "10.78.0.1")


def test_an_instance_removed_before_can_be_purged_once_the_admin_types_its_name(incus, typed):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="removed")})
    typed("bob")
    with pytest.raises(CommandError, match="nothing removed"):
        instances.remove(S, reg, "alice", purge=True)
    assert reg.get("alice").state == "removed"
    typed("alice")
    instances.remove(S, reg, "alice", purge=True)
    assert "alice" not in reg.instances


def test_removing_a_pending_instance_withdraws_its_access(incus, typed, monkeypatch):
    reg = Registry({"alice": Instance("alice", 9120, "lan", address="10.78.0.1", state="pending")})
    incus.containers["alice"] = {"state": "RUNNING"}   # its add stopped after the container started
    published = []
    monkeypatch.setattr(host, "reconcile_access", lambda s, reg: published.append([i.name for i in reg.current()]))
    typed("alice")
    instances.remove(S, reg, "alice")
    assert "alice" not in incus.containers and published == [[]]


def test_removing_an_instance_whose_add_stopped_before_its_container_deletes_its_pool(incus, typed):
    reg = Registry({"alice": Instance("alice", 9120, "local", address="10.78.0.1", state="pending")})
    incus.pools["thinny-alice"] = {"size": 30 * 2**30, "used": 0}
    typed("alice")
    instances.remove(S, reg, "alice")
    assert not incus.pools and reg.get("alice").state == "removed"


def test_a_remove_whose_access_change_failed_runs_again(incus, typed, monkeypatch):
    saved, failures = [], [CommandError("tailscale serve -> exit 1")]
    monkeypatch.setattr(Registry, "save", lambda self, path=None: saved.append(self.get("alice").state))

    def reconcile(s, reg):
        if failures:
            raise failures.pop()
    monkeypatch.setattr(host, "reconcile_access", reconcile)
    incus.containers["alice"] = {"state": "STOPPED"}
    typed("alice")
    with pytest.raises(CommandError, match="tailscale serve"):
        instances.remove(S, Registry({"alice": Instance("alice", 9120, "tailscale", state="disabled")}), "alice")
    assert saved == []   # the registry still says disabled

    instances.remove(S, Registry({"alice": Instance("alice", 9120, "tailscale", state="disabled")}), "alice")
    assert saved == ["removed"]


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
