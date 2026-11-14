import dataclasses
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_instances import LOCKED, S, incus  # noqa: F401 - incus: the fake Incus fixture

from thinny import host, instances, move, render, update
from thinny.registry import Instance, Registry, RegistryError
from thinny.system import CommandError

RELEASE = ("v2", "c0ffee")   # what the incus fixture's server runs


def archive(**changes) -> io.BytesIO:
    """alice's container as her old server exported it (the fake Incus's archives are JSON): stopped with
    autostart off, with port 9127 and address 10.78.0.7 there, signed in to its Webapp."""
    guard = {"type": "disk", "source": str(host.FILES / "model-guard"),
             "path": "/home/alice/.hermes/hermes-agent/plugins/model-guard", "readonly": "true", "shift": "true"}
    container = {"state": "STOPPED", "config": {"boot.autostart": "false"},
                 "account": True, "hermes": True, "unit": True,
                 "devices": {"eth0": {"type": "nic", "nictype": "routed", "ipv4.address": "10.78.0.7",
                                      "host_name": "thinny-9127"},
                             "policy": {"type": "disk", "source": "/etc/hermes", "path": "/etc/hermes",
                                        "readonly": "true", "shift": "true"},
                             "webapp": {"type": "proxy", "listen": "tcp:127.0.0.1:9127",
                                        "connect": "tcp:127.0.0.1:9127"},
                             "model-guard": guard},
                 "files": {"/home/alice/.hermes/.env": f"{instances.AUTH_PREFIX}USERNAME=alice\n"}}
    return io.BytesIO(json.dumps(container | changes).encode())


@pytest.fixture
def target(incus, tmp_path, monkeypatch):
    """This server's registry as another server moves alice here: bob runs here on port 9120 and
    10.78.0.1, Tailscale is off."""
    monkeypatch.setattr(move, "run", incus.run)
    monkeypatch.setattr(move, "STATE_DIR", tmp_path)
    monkeypatch.setattr(update, "UNFINISHED", tmp_path / "update-unfinished")
    monkeypatch.setattr(host, "tailscale_status", lambda: None)
    return Registry({"bob": Instance("bob", 9120, "local", address="10.78.0.1", state="active")})


def refused(reg, message, s=S, access="local", release=RELEASE):
    """Both the check and the move itself refuse alice, and register nothing."""
    names = set(reg.instances)
    for data in (None, archive()):
        with pytest.raises((CommandError, RegistryError), match=message):
            move.receive(s, reg, "alice", access, "", release, render.BUILT_INS, data)
    assert set(reg.instances) == names


def test_a_moved_instance_keeps_its_users_settings_and_gets_this_servers_port_and_address(target, incus, tmp_path,
                                                                                          capsys):
    move.receive(S, target, "alice", "local", "", RELEASE, render.BUILT_INS, archive())
    alice = target.get("alice")
    assert (alice.state, alice.moved, alice.port, alice.address, alice.url, alice.built_ins) == \
        ("active", True, 9121, "10.78.0.2", "http://alice.localhost:9121/", render.BUILT_INS)
    assert incus.ran(f"incus import {tmp_path}/alice.tar.gz alice --storage default")
    assert incus.imported_mode == 0o600 and not (tmp_path / "alice.tar.gz").exists()   # it holds alice's secrets
    # the old server turned autostart off for the move; the profile's applies again
    assert incus.index("incus import") < incus.index("incus config unset alice boot.autostart") \
        < incus.index("incus start alice")
    assert "boot.autostart" not in incus.containers["alice"]["config"]
    devices = incus.containers["alice"]["devices"]
    assert (devices["eth0"]["ipv4.address"], devices["eth0"]["host_name"]) == ("10.78.0.2", "thinny-9121")
    assert (devices["webapp"]["listen"], devices["webapp"]["connect"]) == ("tcp:127.0.0.1:9121",) * 2
    assert incus.index("incus config device set alice eth0") < incus.index("incus start alice")
    # this server's policy for alice, also when its old one shared /etc/hermes
    assert devices["policy"]["source"] == str(host.POLICIES / "alice")
    assert incus.index("incus config device set alice policy") < incus.index("incus start alice")
    # its users' language, model, look, password and Hermes came along
    assert not incus.ran("display.language") and not incus.ran("hermes config set model.")
    assert not incus.ran("write_preferences") and not incus.ran("hermes-install.sh")
    out = capsys.readouterr().out
    assert "Password: unchanged" in out
    assert "if alice did not use openai-codex on its old server, sign it in: thinny instance login alice" in out


def test_on_a_server_with_the_model_lock_the_model_it_had_goes_at_once(target, incus, monkeypatch):
    # Hermes would leave it as invalid YAML under the lock, which refuses the unset itself. Its gateway
    # starts with the container, so before waiting for the boot to finish.
    def unsets():
        return [i for i, c in enumerate(incus.calls) if 'HERMES_MANAGED_DIR="$d" hermes config unset' in c]
    wait = "incus exec alice -- systemctl is-system-running --wait"
    monkeypatch.setattr(instances.time, "sleep", lambda s: None)
    with pytest.raises(CommandError, match="cannot reach the internet"):
        move.receive(LOCKED, target, "alice", "local", "", RELEASE, render.BUILT_INS, archive(internet=False))
    assert [incus.calls[i].rsplit(" ", 1)[1] for i in unsets()] == \
        ["model.provider", "model.default", "agent.reasoning_effort"]
    assert incus.index("incus start alice") < unsets()[0] < unsets()[-1] < incus.index(wait)

    # the add that finishes it unsets them too, although its container runs already
    incus.calls.clear()
    incus.containers["alice"]["internet"] = True
    instances.add(LOCKED, target, "alice", "local")
    assert len(unsets()) == 3 and unsets()[-1] < incus.index(wait) and not incus.ran("incus start")
    assert target.get("alice").state == "active"


def test_a_server_without_hermes_yet_takes_the_release_and_gives_new_built_ins(target, incus, monkeypatch):
    released = []
    monkeypatch.setattr(host, "release", lambda: released[-1] if released else None)
    monkeypatch.setattr(host, "write_release", lambda tag, commit: released.append((tag, commit)))
    reg = Registry()
    move.receive(S, reg, "alice", "local", "", ("v1", "c1"), "older", archive())
    assert released == [("v1", "c1")]
    # like an update: the built-ins it lacks, turned on
    assert incus.ran("bash /tmp/hermes-install.sh --channel stable --commit c1 --non-interactive")
    assert incus.ran("hermes plugins install https://github.com/melounvitek/hermes-3310 --ref ")
    assert incus.ran(" --force --yes-deps --enable") and not incus.ran("--no-enable")
    assert (reg.get("alice").state, reg.get("alice").built_ins) == ("active", render.BUILT_INS)
    assert not incus.ran("display.language")


def test_instance_add_finishes_a_moved_instance_whose_setup_failed_here(target, incus, monkeypatch):
    monkeypatch.setattr(instances.time, "sleep", lambda s: None)
    with pytest.raises(CommandError, match="cannot reach the internet"):
        move.receive(S, target, "alice", "local", "", RELEASE, render.BUILT_INS, archive(internet=False))
    alice = target.get("alice")
    assert (alice.state, alice.moved, alice.built_ins) == ("pending", True, render.BUILT_INS)

    # An update here may have changed the release since, which an update would not roll out to alice, or an
    # earlier resume may have stopped halfway through Hermes's install: it installs again.
    monkeypatch.setattr(host, "release", lambda: ("v3", "c3"))
    incus.containers["alice"]["internet"] = True
    instances.add(S, target, "alice", "local")
    assert alice.state == "active"
    assert not incus.ran("display.language")
    assert incus.ran("bash /tmp/hermes-install.sh --channel stable --commit c3 --non-interactive")
    # the plugins its users turned off stay off
    assert incus.ran(" --force --yes-deps --no-enable") and not incus.ran("--yes-deps --enable")


def test_a_container_imported_here_is_registered_before_anything_else_can_fail(target, incus, monkeypatch):
    def disk_full(tag, commit):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(host, "release", lambda: None)
    monkeypatch.setattr(host, "write_release", disk_full)
    reg = Registry()
    with pytest.raises(OSError):
        move.receive(S, reg, "alice", "local", "", ("v1", "c1"), render.BUILT_INS, archive())
    assert "alice" in incus.containers
    assert (reg.get("alice").state, reg.get("alice").moved) == ("pending", True)   # instance add finishes it


def test_a_failed_import_leaves_nothing_here_so_the_move_can_be_tried_again(target, incus, tmp_path):
    with pytest.raises(CommandError, match="incus import"):
        move.receive(S, target, "alice", "local", "", RELEASE, render.BUILT_INS, io.BytesIO(b"\x1f\x8b cut off"))
    assert "alice" not in target.instances and not (tmp_path / "alice.tar.gz").exists()
    move.receive(S, target, "alice", "local", "", RELEASE, render.BUILT_INS, archive())
    assert target.get("alice").state == "active"


def test_a_name_this_server_has_or_had_is_refused(target, incus, monkeypatch):
    target.add("alice", "local", (9120, 9199)).state = "disabled"
    refused(target, "alice exists on this server too; names are not reused")
    del target.instances["alice"]
    incus.containers["alice"] = {"state": "STOPPED"}
    refused(target, "an Incus container 'alice' already exists")
    del incus.containers["alice"]
    monkeypatch.setattr(instances, "uid_of", lambda name: 100)
    refused(target, "'alice' is a system account name")
    assert not incus.ran("incus import")


def test_another_release_or_an_unfinished_update_is_refused(target, incus, tmp_path):
    refused(target, "run thinny update on both servers first", release=("v1", "c1"))
    (tmp_path / "update-unfinished").write_text("v3 c3\n")
    refused(target, "run thinny update on both servers first")
    assert not incus.ran("incus import")


def test_what_this_server_cannot_give_is_refused(target, incus):
    refused(target, "set access.lan_address in site.toml first", access="lan")
    refused(target, "Tailscale is not running on this server", access="tailscale")
    refused(target, "no free Webapp port left", s=dataclasses.replace(S, port_range=(9120, 9120)))
    assert not incus.ran("incus import")


class Other:
    """The other server, as `remote` reaches it: refuses the names in `refuses` and those it has, takes
    what it gets except the names in `fails`, and for the names in `lost` its answer never arrives."""

    def __init__(self):
        self.calls, self.received = [], {}
        self.refuses, self.fails, self.lost, self.interrupted = set(), set(), set(), set()

    def remote(self, to, args, stdin=None):
        self.calls.append((to, args))
        name = args[2]
        if "--check" in args:
            return 1 if name in self.refuses or name in self.received else 0
        if name in self.fails:
            return 1
        if name in self.interrupted:   # the admin presses Ctrl-C
            raise KeyboardInterrupt
        self.received[name] = json.load(stdin)
        return 255 if name in self.lost else 0


@pytest.fixture
def source(incus, tmp_path, monkeypatch):
    """This server: alice (local) and bob (url) run, carol is disabled, dave pending. Returns the
    registry and the other server."""
    monkeypatch.setattr(move, "run", incus.run)
    monkeypatch.setattr(move, "STATE_DIR", tmp_path)
    monkeypatch.setattr(update, "UNFINISHED", tmp_path / "update-unfinished")
    other = Other()
    monkeypatch.setattr(move, "remote", other.remote)
    for name, state in (("alice", "RUNNING"), ("bob", "RUNNING"), ("carol", "STOPPED"), ("dave", "RUNNING")):
        incus.containers[name] = {"state": state, "devices": {}}
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active", built_ins="b1"),
                    "bob": Instance("bob", 9121, "url", url="https://bob.example.org/", state="active", built_ins="b1"),
                    "carol": Instance("carol", 9122, "local", state="disabled"),
                    "dave": Instance("dave", 9123, "local")})
    return reg, other


def receive(name, *args):
    return ["instance", "receive", name, *args]


def test_every_active_instance_moves_one_at_a_time_and_stays_here_disabled(source, incus, tmp_path):
    reg, other = source
    move.move(S, reg, [], "admin@new")
    alice = ["--access", "local", "--release", "v2", "c0ffee", "--built-ins", "b1"]
    bob = ["--access", "url", "--url", "https://bob.example.org/", "--release", "v2", "c0ffee", "--built-ins", "b1"]
    # the other server checks both before either stops
    assert [args for _, args in other.calls] == [receive("alice", *alice, "--check"), receive("bob", *bob, "--check"),
                                                 receive("alice", *alice), receive("bob", *bob)]
    assert {to for to, _ in other.calls} == {"admin@new"}
    assert other.received["alice"]["state"] == other.received["bob"]["state"] == "STOPPED"
    assert incus.index("incus config set alice boot.autostart=false") < incus.index("incus stop alice") \
        < incus.index("incus export alice") < incus.index("incus stop bob")
    export = incus.calls[incus.index("incus export alice ")].split()
    assert Path(export[3]).parent.parent == tmp_path and export[3].endswith("/alice.tar.gz") and "--instance-only" in export
    # in a directory of its own that only root can enter: it holds the instance's secrets
    assert incus.exported_mode == 0o700 and not list(tmp_path.glob("tmp*"))
    assert [inst.state for inst in reg.instances.values()] == ["disabled", "disabled", "disabled", "pending"]
    assert incus.containers["alice"]["state"] == incus.containers["bob"]["state"] == "STOPPED"


def test_nothing_stops_unless_the_other_server_can_take_every_instance(source, incus):
    reg, other = source
    other.refuses.add("bob")
    with pytest.raises(CommandError, match="admin@new cannot take bob"):
        move.move(S, reg, ["alice", "bob"], "admin@new")
    assert not incus.ran("boot.autostart") and not incus.ran("incus stop")
    assert reg.get("alice").state == reg.get("bob").state == "active"


@pytest.mark.parametrize("failure", ["export directory", "export", "the other server", "Ctrl-C"])
def test_a_failed_move_starts_the_instance_here_again_when_the_other_server_lacks_it(source, incus, tmp_path,
                                                                                    monkeypatch, failure):
    reg, other = source
    if failure == "export directory":
        monkeypatch.setattr(move, "STATE_DIR", tmp_path / "missing")
    elif failure == "export":
        incus.containers["alice"]["export_fails"] = True
    elif failure == "Ctrl-C":
        other.interrupted.add("alice")
    else:
        other.fails.add("alice")
    with pytest.raises(CommandError, match="moving alice failed; it runs here again"):
        move.move(S, reg, ["alice", "bob"], "admin@new")
    assert incus.index("incus stop alice") < incus.index("incus config unset alice boot.autostart") \
        < incus.index("incus start alice")
    assert incus.containers["alice"]["state"] == "RUNNING" and reg.get("alice").state == "active"
    assert not incus.ran("incus stop bob") and not list(tmp_path.glob("tmp*"))


def test_an_instance_that_may_have_reached_the_other_server_stays_stopped_with_what_to_do(source, incus):
    reg, other = source
    other.lost.add("alice")
    with pytest.raises(CommandError) as e:
        move.move(S, reg, ["alice", "bob"], "admin@new")
    # also when the other server has the container but did not register it
    for hint in ("thinny instance list", "thinny instance add alice", "thinny instance disable alice",
                 "incus delete alice", "incus config unset alice boot.autostart && incus start alice"):
        assert hint in str(e.value)
    assert incus.containers["alice"]["state"] == "STOPPED" and reg.get("alice").state == "active"
    assert not incus.ran("incus start") and not incus.ran("incus stop bob")


def test_only_active_instances_move_and_not_during_an_unfinished_update(source, incus, tmp_path):
    reg, other = source
    with pytest.raises(RegistryError, match="carol is disabled"):
        move.move(S, reg, ["alice", "carol"], "admin@new")
    (tmp_path / "update-unfinished").write_text("v3 c3\n")
    with pytest.raises(CommandError, match="run thinny update on both servers first"):
        move.move(S, reg, ["alice"], "admin@new")
    assert other.calls == [] and not incus.ran("incus stop")


@pytest.mark.parametrize("uid, sudo", [("0", False), ("1000", True)])
def test_the_other_thinny_runs_over_ssh_as_root_or_through_sudo_without_a_password(tmp_path, monkeypatch, uid, sudo):
    # A fake ssh hands the command to a shell, as sshd does; there, id, sudo and thinny are fakes.
    bin = tmp_path / "bin"
    bin.mkdir()
    fakes = {bin / "id": f"#!/bin/sh\necho {uid}\n",
             bin / "sudo": '#!/bin/sh\n[ "$1" = -n ] && shift && SUDO=1 exec "$@"\n',
             tmp_path / "thinny": f"#!{sys.executable}\nimport json, os, sys\n"
                                  f"json.dump([sys.argv[1:], 'SUDO' in os.environ, sys.stdin.buffer.read().hex()], "
                                  f"open({str(tmp_path / 'got')!r}, 'w'))\nsys.exit(7)\n"}
    for path, text in fakes.items():
        path.write_text(text)
        path.chmod(0o755)
    monkeypatch.setattr(host, "THINNY", str(tmp_path / "thinny"))
    real_run, started = subprocess.run, []

    def ssh(cmd, stdin):
        started.append((cmd[:-1], stdin))
        return real_run(["sh", "-c", cmd[-1]], stdin=stdin, env={"PATH": f"{bin}:/usr/bin:/bin"})
    monkeypatch.setattr(move.subprocess, "run", ssh)

    def got():
        return json.loads((tmp_path / "got").read_text())

    args = ["instance", "receive", "it's", "a b", "$HOME", "", "x;y"]
    assert move.remote("admin@new", args) == 7
    assert got() == [args, sudo, ""]
    (tmp_path / "alice.tar.gz").write_bytes(b"\x1f\x8b\x00\xff")
    with (tmp_path / "alice.tar.gz").open("rb") as f:
        move.remote("admin@new", ["instance"], stdin=f)
    assert got() == [["instance"], sudo, "1f8b00ff"]
    # no questions: nothing could answer them
    # a dead connection ends with 255, without a limit on the time a large import takes
    assert [cmd for cmd, _ in started] == [["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
                                            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
                                            "--", "admin@new"]] * 2
    assert started[0][1] == subprocess.DEVNULL
