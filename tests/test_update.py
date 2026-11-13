import dataclasses
import datetime as dt
import json
import re
import subprocess
import tarfile

import pytest

from hermes_manager import host, people, system, update
from hermes_manager.registry import Person, Registry
from hermes_manager.settings import Settings
from hermes_manager.system import CommandError

S = Settings(model="m1", allowed_models=("m1",), lan_address="10.0.0.5")
UPDATE = "hermes update --yes --no-gateway-restart"


class Server:
    """v1 installed, v2 released `age` hours ago; every step is recorded instead of run."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.calls = []
        self.installed = {"v1"}
        self.age = 72
        self.images = {"alice": "old", "bob": "new"}  # alice's pull brings a new image, bob has it

    def status(self):
        return json.loads((self.tmp / "status.json").read_text())

    def record(self, step):
        return lambda *args, **kw: self.calls.append(step)

    def hermes_root(self, args, **kw):
        self.calls.append("hermes " + " ".join(args))
        self.installed = {"v2"}
        return subprocess.CompletedProcess(args, 0, "Updated to v2\n", "")

    def as_user(self, name, cmd, **kw):
        self.calls.append(f"{name}: {' '.join(cmd)}")
        out = ""
        if cmd[1:3] == ["image", "inspect"]:
            out = self.images[name]
        elif cmd[1] == "pull":
            self.images[name] = "new"
        elif cmd[1] == "ps":
            out = "hermes-1\nsomething-else\n"
        elif cmd[1] == "exec":
            out = "000"  # bob's running sandbox has no network
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = Server(tmp_path)
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "hermes-manager.log")
    monkeypatch.setattr(update, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(update, "UNFINISHED", tmp_path / "update-unfinished")
    monkeypatch.setattr(update, "REBOOT_REQUIRED", tmp_path / "reboot-required")
    monkeypatch.setattr(host, "latest_release",
                        lambda: ("v2", dt.datetime.now(dt.UTC) - dt.timedelta(hours=srv.age)))
    monkeypatch.setattr(host, "installed_tags", lambda: set(srv.installed))
    monkeypatch.setattr(update, "backup", srv.record("backup"))
    monkeypatch.setattr(update, "hermes_root", srv.hermes_root)
    for step in ("fix_permissions", "ensure_plugins", "build_webapp"):
        monkeypatch.setattr(host, step, srv.record(step))
    monkeypatch.setattr(people, "hermes", lambda name, args, **kw: srv.calls.append(f"{name}: hermes {' '.join(args)}"))
    monkeypatch.setattr(people, "restart_all",
                        lambda ps, reg: srv.calls.append("restart " + " ".join(p.name for p in ps)))
    return srv


def registry():
    return Registry({"alice": Person("alice", 9120, "lan", state="active"),
                     "bob": Person("bob", 9121, "lan", state="active"),
                     "carol": Person("carol", 9122, "lan", state="pending")})


@pytest.mark.parametrize("installed, age, auto_update, now, result, detail", [
    ({"v2"}, 72, True, False, "up-to-date", "v2"),
    ({"v1"}, 1, True, False, "waiting", "v2 is 1 h old, waiting for 48 h"),
    ({"v1"}, 1, True, True, "ok", "updated to v2"),
    ({"v1"}, 72, False, False, "available", "v2; automatic updates are off"),
    ({"v1"}, 72, False, True, "ok", "updated to v2"),
])
def test_only_a_due_release_is_installed(server, installed, age, auto_update, now, result, detail):
    server.installed, server.age = installed, age
    assert update.run(dataclasses.replace(S, auto_update=auto_update), Registry(), now=now) == 0
    status = server.status()
    assert (status["result"], status["detail"]) == (result, detail)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", status["time"])
    assert (UPDATE in server.calls) == (result == "ok")
    assert server.calls[-1] == "fix_permissions"


def test_check_only_reports(server):
    assert update.run(S, registry(), check=True) == 0
    assert server.calls == []
    assert not (server.tmp / "status.json").exists()


def test_full_update(server):
    (server.tmp / "reboot-required").touch()
    assert update.run(S, registry()) == 0
    assert server.status()["detail"] == "updated to v2; the OS needs a reboot"
    calls = server.calls
    # Backup before the code changes; people's environments follow the new lock before their
    # gateways restart; the sandboxes are refreshed last. Pending people are left alone.
    assert calls.index("backup") < calls.index(UPDATE) < calls.index("build_webapp")
    assert calls.index("build_webapp") < calls.index("alice: hermes pm install --extra all") < calls.index(
        "restart alice bob")
    assert not any(c.startswith("carol") for c in calls)
    assert not (server.tmp / "update-unfinished").exists()


def test_an_interrupted_rollout_is_finished_without_github(server, monkeypatch):
    def restart_all(ps, reg):
        raise CommandError("bob: gateway not active, Webapp HTTP 000")
    monkeypatch.setattr(people, "restart_all", restart_all)
    assert update.run(S, registry()) == 1
    assert (server.status()["result"], server.status()["detail"]) == (
        "FAILED", "bob: gateway not active, Webapp HTTP 000")

    def offline():
        raise OSError("GitHub is down")
    monkeypatch.setattr(host, "latest_release", offline)
    monkeypatch.setattr(people, "restart_all", lambda ps, reg: server.calls.append("restart"))
    server.calls.clear()
    assert update.run(S, registry()) == 0
    assert (server.status()["result"], server.status()["detail"]) == ("ok", "finished the interrupted update to v2")
    assert UPDATE not in server.calls and "restart" in server.calls
    assert not (server.tmp / "update-unfinished").exists()


def test_a_cut_off_code_update_is_retried_whatever_its_age(server, monkeypatch):
    def failing_update(args, **kw):
        raise CommandError("hermes update -> exit 1")
    monkeypatch.setattr(update, "hermes_root", failing_update)
    assert update.run(S, registry()) == 1

    monkeypatch.setattr(update, "hermes_root", server.hermes_root)
    server.age = 1   # a newer release appeared meanwhile; the cut-off update still goes on
    assert update.run(S, registry()) == 0
    assert server.status()["detail"] == "updated to v2"


def test_a_code_update_that_got_through_is_rolled_out_without_github(server, monkeypatch):
    (server.tmp / "update-unfinished").write_text("code v2\n")
    server.installed = {"v2"}   # hermes update finished, the run stopped before recording it

    def offline():
        raise OSError("GitHub is down")
    monkeypatch.setattr(host, "latest_release", offline)
    assert update.run(dataclasses.replace(S, auto_update=False), registry()) == 0
    assert server.status()["detail"] == "finished the interrupted update to v2"
    assert UPDATE not in server.calls


def test_a_failed_backup_leaves_nothing_to_resume(server, monkeypatch):
    def failing_backup(s):
        raise OSError("disk full")
    monkeypatch.setattr(update, "backup", failing_backup)
    assert update.run(S, registry()) == 1
    assert not (server.tmp / "update-unfinished").exists()
    assert UPDATE not in server.calls


def test_backup_keeps_the_newest_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(host, "MANAGED", tmp_path / "etc-hermes")
    monkeypatch.setattr(host, "CHECKOUT", tmp_path / "checkout")
    (tmp_path / "etc-hermes").mkdir()
    (tmp_path / "etc-hermes/config.yaml").write_text("x")
    for name in ("model-guard", "lang-xx"):
        (tmp_path / "checkout/plugins" / name).mkdir(parents=True)
    (tmp_path / "backups").mkdir()
    for i in range(14):
        (tmp_path / f"backups/2000-01-01-00{i:02}.tgz").touch()

    path = update.backup(dataclasses.replace(S, language_pack="lang-xx"))
    backups = sorted((tmp_path / "backups").iterdir())
    assert len(backups) == 14 and backups[0].name == "2000-01-01-0001.tgz" and backups[-1] == path
    with tarfile.open(path) as tar:
        assert {"etc-hermes/config.yaml", "plugins/model-guard", "plugins/lang-xx"} <= set(tar.getnames())
