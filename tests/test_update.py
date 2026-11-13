import dataclasses
import datetime as dt
import json
import re

import pytest

from thinny import host, instances, render, system, update
from thinny.registry import Instance, Registry
from thinny.settings import Settings
from thinny.system import CommandError

S = Settings(model="m1", allowed_models=("m1",), lan_address="10.0.0.5")


class Server:
    """All instances run v1 (commit c1); v2 (commit c2) came out `age` hours ago. Hermes installs and
    gateway restarts are recorded instead of run; `broken` instances fail theirs."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.calls = []
        self.release = ("v1", "c1")
        self.commits = {"alice": "c1", "bob": "c1", "carol": "c1"}
        self.age = 72
        self.broken = set()
        self.stops_after_checkout = set()   # their installer moves the checkout, then fails
        self.claude, self.claude_broken = [], set()   # Claude Code updates

    def status(self):
        return json.loads((self.tmp / "status.json").read_text())

    def install_hermes(self, s, name, commit, enable):
        if name in self.broken:
            raise CommandError(f"{name}: installer -> exit 1")
        self.calls.append(f"install {name} {commit}" + (" enable" if enable else ""))
        self.commits[name] = commit
        if name in self.stops_after_checkout:
            raise CommandError(f"{name}: hermes webapp --build-only -> exit 1")

    def restart(self, s, inst):
        if inst.name in self.broken:
            raise CommandError(f"{inst.name}: gateway not active, Webapp HTTP 000")
        self.calls.append(f"restart {inst.name}")

    def as_user(self, name, cmd, **kw):
        assert cmd == ["claude", "update"]
        if name in self.claude_broken:
            raise CommandError(f"{name}: claude update -> exit 1")
        self.claude.append(name)


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = Server(tmp_path)
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "thinny.log")
    monkeypatch.setattr(update, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(update, "UNFINISHED", tmp_path / "update-unfinished")
    monkeypatch.setattr(update, "REBOOT_REQUIRED", tmp_path / "reboot-required")
    monkeypatch.setattr(instances, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(host, "latest_release",
                        lambda: ("v2", dt.datetime.now(dt.UTC) - dt.timedelta(hours=srv.age)))
    monkeypatch.setattr(host, "commit_of", lambda tag: {"v1": "c1", "v2": "c2"}[tag])
    monkeypatch.setattr(host, "release", lambda: srv.release)
    monkeypatch.setattr(host, "write_release", lambda tag, commit: setattr(srv, "release", (tag, commit)))
    monkeypatch.setattr(instances, "installed_commit", lambda name: srv.commits[name])
    monkeypatch.setattr(instances, "install_hermes", srv.install_hermes)
    monkeypatch.setattr(instances, "ensure_packages", lambda name: srv.calls.append(f"packages {name}"))
    monkeypatch.setattr(instances, "ensure_account", lambda name: srv.calls.append(f"account {name}"))
    monkeypatch.setattr(instances, "_gateway_up", srv.restart)
    monkeypatch.setattr(update, "as_user", srv.as_user)
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    return srv


def registry():
    """Every instance has the built-ins of this thinny."""
    return Registry({"alice": Instance("alice", 9120, "lan", state="active", built_ins=render.BUILT_INS),
                     "bob": Instance("bob", 9121, "lan", state="active", built_ins=render.BUILT_INS),
                     "carol": Instance("carol", 9122, "lan", state="pending")})


@pytest.mark.parametrize("release, age, auto_update, now, result, detail", [
    (("v2", "c2"), 72, True, False, "up-to-date", "v2"),
    (("v1", "c1"), 1, True, False, "waiting", "v2 is 1 h old, waiting for 48 h"),
    (("v1", "c1"), 1, True, True, "ok", "updated to v2"),
    (("v1", "c1"), 72, False, False, "available", "v2; automatic updates are off"),
    (("v1", "c1"), 72, False, True, "ok", "updated to v2"),
])
def test_only_a_due_release_is_installed(server, release, age, auto_update, now, result, detail):
    server.release, server.age = release, age
    assert update.run(dataclasses.replace(S, auto_update=auto_update), registry(), now=now) == 0
    status = server.status()
    assert (status["result"], status["detail"]) == (result, detail)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", status["time"])
    assert ("install alice c2" in server.calls) == (result == "ok")


def test_check_only_reports(server):
    assert update.run(S, registry(), dry_run=True) == 0
    assert server.calls == [] and server.claude == []
    assert not (server.tmp / "status.json").exists()


def test_every_instance_moves_to_the_exact_release_one_at_a_time(server):
    (server.tmp / "reboot-required").touch()
    assert update.run(S, registry()) == 0
    assert server.status()["detail"] == "updated to v2; the OS needs a reboot"
    # each instance is restarted before the next one is touched; pending instances are left alone
    assert server.calls == ["packages alice", "account alice", "install alice c2", "restart alice",
                            "packages bob", "account bob", "install bob c2", "restart bob"]
    assert server.release == ("v2", "c2")
    assert not (server.tmp / "update-unfinished").exists()


def test_nothing_to_update_before_the_first_instance(server):
    server.release = None
    assert update.run(S, Registry()) == 0
    assert server.status()["detail"] == "no instance has Hermes yet"


def test_an_interrupted_update_is_finished_without_github(server, monkeypatch):
    server.broken.add("bob")
    assert update.run(S, registry()) == 1
    assert server.status() | {"time": ""} == {
        "time": "", "result": "FAILED", "detail": "bob: installer -> exit 1"}
    assert server.release == ("v1", "c1")   # new instances still get v1 meanwhile

    def offline():
        raise OSError("GitHub is down")
    monkeypatch.setattr(host, "latest_release", offline)
    server.broken.clear()
    server.calls.clear()
    assert update.run(dataclasses.replace(S, auto_update=False), registry()) == 0
    assert server.status()["detail"] == "finished the interrupted update to v2"
    assert server.calls == ["packages bob", "account bob", "install bob c2", "restart bob"]   # alice is done
    assert server.release == ("v2", "c2")


def test_a_resume_redoes_the_instance_it_stopped_at_although_its_checkout_moved(server):
    server.stops_after_checkout.add("alice")
    assert update.run(S, registry()) == 1
    assert server.commits["alice"] == "c2"   # the checkout moved, the Webapp build failed

    server.stops_after_checkout.clear()
    server.calls.clear()
    assert update.run(S, registry()) == 0
    assert server.calls == ["packages alice", "account alice", "install alice c2", "restart alice",
                            "packages bob", "account bob", "install bob c2", "restart bob"]


def test_the_instance_it_stopped_at_comes_first_even_when_others_changed_their_hermes(server):
    server.stops_after_checkout.add("bob")
    assert update.run(S, registry()) == 1
    server.stops_after_checkout.clear()
    server.commits["alice"] = "own"   # alice's users moved its Hermes meanwhile
    server.broken.add("alice")
    server.calls.clear()
    assert update.run(S, registry()) == 1
    assert server.calls == ["packages bob", "account bob", "install bob c2", "restart bob",
                            "packages alice", "account alice"]

    server.broken.clear()
    server.calls.clear()
    assert update.run(S, registry()) == 0
    assert server.calls == ["packages alice", "account alice", "install alice c2", "restart alice"]


def test_no_other_instance_is_touched_while_a_restart_keeps_failing(server, monkeypatch):
    def broken(s, inst):
        raise CommandError(f"{inst.name}: gateway not active")
    monkeypatch.setattr(instances, "_gateway_up", broken)
    assert update.run(S, registry()) == 1
    assert update.run(S, registry()) == 1
    assert server.commits["bob"] == "c1"

    monkeypatch.setattr(instances, "_gateway_up", server.restart)
    server.calls.clear()
    assert update.run(S, registry()) == 0
    assert server.calls == ["packages alice", "account alice", "install alice c2", "restart alice",
                            "packages bob", "account bob", "install bob c2", "restart bob"]


@pytest.mark.parametrize("release, age", [(("v2", "c2"), 72), (("v1", "c1"), 1)],
                         ids=["up-to-date", "a newer release waits"])
def test_new_built_ins_reach_every_instance_at_the_release_it_runs(server, release, age):
    server.release, server.age = release, age
    tag, commit = release
    server.commits = {"alice": commit, "bob": commit, "carol": commit}
    reg = registry()
    reg.get("alice").built_ins = "older"
    assert update.run(S, reg) == 0
    assert server.status()["detail"] == f"new built-ins at {tag}"
    # turning the plugins on again only now: a plain Hermes update keeps what users turned off
    assert server.calls == ["packages alice", "account alice", f"install alice {commit} enable", "restart alice"]
    assert reg.get("alice").built_ins == render.BUILT_INS
    assert server.release == release


def test_new_built_ins_are_only_reported_without_automatic_updates(server):
    server.release = ("v2", "c2")
    reg = registry()
    reg.get("alice").built_ins = ""   # added by an older thinny
    assert update.run(dataclasses.replace(S, auto_update=False), reg, now=False) == 0
    assert (server.status()["result"], server.status()["detail"]) == (
        "available", "new built-ins; automatic updates are off")
    assert server.calls == []


def test_an_instance_keeps_its_old_built_ins_until_its_gateway_is_back(server):
    server.release = ("v2", "c2")
    server.commits = {"alice": "c2", "bob": "c2", "carol": "c2"}
    reg = registry()
    reg.get("alice").built_ins = "older"
    server.broken.add("alice")
    assert update.run(S, reg) == 1
    assert reg.get("alice").built_ins == "older"


def test_claude_code_is_updated_every_night_whatever_happens_to_hermes(server):
    server.broken.add("bob")
    server.claude_broken.add("alice")
    assert update.run(S, registry()) == 1
    assert server.status()["detail"] == "bob: installer -> exit 1"   # a failed Claude update is only logged
    assert server.claude == ["bob"]   # active instances only, alice's failed

    server.release, server.broken = ("v2", "c2"), set()
    server.commits = {"alice": "c2", "bob": "c2", "carol": "c2"}
    server.claude.clear()
    assert update.run(S, registry()) == 0
    assert server.claude == ["bob"]
