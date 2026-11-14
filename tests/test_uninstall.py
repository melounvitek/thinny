import json
import os
import subprocess

import pytest

from thinny import cli, host, instances, render, settings, system, uninstall, update
from thinny.registry import Instance, Registry
from thinny.system import CommandError

UNITS = ("thinny-update.service", "thinny-update.timer", "thinny-trace.service", "incus.service.d/thinny.conf")


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def thinny_files():
    """thinny's files on the fake server that are still there."""
    paths = [settings.SITE_TOML.parent, host.MANAGED, system.STATE_DIR, system.LOG_FILE, host.SYSCTL_FILE,
             *(host.SYSTEMD / unit for unit in UNITS), host.SYSTEMD / "incus.service.d", host.CADDYFILE,
             uninstall.SUDOERS, uninstall.CLONE, uninstall.LIB, uninstall.COMMAND, update.SELF_UPDATE_LOCK,
             system.LOCK_FILE]
    return [path for path in paths if os.path.lexists(path)]


class Server:
    """Plays Incus, Tailscale and nft; records every command and thinny's files that were there when it ran.
    A command starting with a key of `fails` gets its (output, exit code); deleting the storage pool fails while
    it holds an image."""

    def __init__(self):
        self.calls, self.files = [], []
        self.fails = {}
        self.containers = {"alice": "RUNNING", "bob": "STOPPED", "carol": "RUNNING"}
        self.profile = self.pool = self.table = True
        self.images = [{"fingerprint": "f00d", "cached": True}]   # Debian, downloaded for the containers
        self.tailscale = {"BackendState": "Running"}
        self.served = {9120, 9121}
        self.answers = ["office-server"]

    def run(self, cmd, *, check=True, **kw):
        self.calls.append(" ".join(cmd))
        self.files.append(thinny_files())
        failing = [answer for prefix, answer in self.fails.items() if self.calls[-1].startswith(prefix)]
        out, code = failing[0] if failing else self.answer(cmd)
        if check and code:
            raise CommandError(f"{' '.join(cmd)} -> exit {code}")
        return subprocess.CompletedProcess(cmd, code, out, "")

    def answer(self, cmd):
        if cmd[:2] == ["incus", "list"]:
            state = self.containers.get(cmd[-1].strip("^$"))
            return (state + "\n" if state else ""), 0
        if cmd[:3] == ["incus", "delete", "--force"]:
            del self.containers[cmd[3]]
        elif cmd[:3] == ["incus", "profile", "list"]:
            return "default\n" + "thinny-instance\n" * self.profile, 0
        elif cmd[:3] == ["incus", "profile", "delete"]:
            self.profile = False
        elif cmd[:3] == ["incus", "storage", "list"]:
            return "default\n" * self.pool, 0
        elif cmd[:3] == ["incus", "image", "list"]:
            return json.dumps(self.images), 0
        elif cmd[:3] == ["incus", "image", "delete"]:
            self.images = [image for image in self.images if image["fingerprint"] != cmd[3]]
        elif cmd[:3] == ["incus", "storage", "delete"]:
            if self.images:
                return "", 1
            self.pool = False
        elif cmd[:3] == ["tailscale", "serve", "status"]:
            if not self.served:
                return "{}\n", 0
            return json.dumps({"TCP": {str(port): {"HTTPS": True} for port in self.served}}), 0
        elif cmd[:2] == ["tailscale", "serve"]:
            self.served.discard(int(cmd[2].removeprefix("--https=")))
        elif cmd[:3] == ["nft", "list", "tables"]:
            return "table inet filter\n" + "table inet thinny\n" * self.table, 0
        elif cmd[:2] == ["nft", "delete"]:
            self.table = False
        return "", 0

    def index(self, prefix):
        """Position of the first command starting with `prefix`."""
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def input(self, prompt=""):
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """thinny installed by bin/install on the server office-server, on a terminal, with an instance in each
    state: carol's add stopped after creating her container, dave's before."""
    srv = Server()
    for module in (system, instances, host):
        monkeypatch.setattr(module, "run", srv.run)
    monkeypatch.setattr(host, "tailscale_status", lambda: srv.tailscale)
    monkeypatch.setattr(settings, "SITE_TOML", write(tmp_path / "etc/thinny/site.toml"))
    write(tmp_path / "etc/thinny/thinny.nft")
    monkeypatch.setattr(host, "MANAGED", write(tmp_path / "etc/hermes/config.yaml").parent)
    monkeypatch.setattr(system, "STATE_DIR", tmp_path / "var/lib/thinny")
    Registry({"alice": Instance("alice", 9120, "lan", state="active"),
              "bob": Instance("bob", 9121, "tailscale", state="disabled"),
              "carol": Instance("carol", 9122, "local"),
              "dave": Instance("dave", 9123, "local")}).save(system.STATE_DIR / "instances.json")
    load = Registry.load
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: load(system.STATE_DIR / "instances.json")))
    monkeypatch.setattr(system, "LOG_FILE", write(tmp_path / "var/log/thinny.log"))
    monkeypatch.setattr(host, "SYSCTL_FILE", write(tmp_path / "etc/sysctl.d/90-thinny.conf"))
    monkeypatch.setattr(host, "SYSTEMD", tmp_path / "etc/systemd/system")
    for unit in UNITS:
        write(host.SYSTEMD / unit)
    monkeypatch.setattr(host, "CADDYFILE", write(tmp_path / "etc/caddy/Caddyfile", f"# {render.HEADER}\n"))
    monkeypatch.setattr(uninstall, "SUDOERS", write(tmp_path / "etc/sudoers.d/thinny-checks"))
    monkeypatch.setattr(uninstall, "CLONE", write(tmp_path / "usr/local/src/thinny/install.sh").parent)
    monkeypatch.setattr(uninstall, "LIB", write(tmp_path / "usr/local/lib/thinny/bin/thinny").parent.parent)
    write(uninstall.LIB / "source", f"{uninstall.CLONE}\n")
    monkeypatch.setattr(uninstall, "COMMAND", tmp_path / "usr/local/sbin/thinny")
    uninstall.COMMAND.parent.mkdir(parents=True)
    uninstall.COMMAND.symlink_to(uninstall.LIB / "bin/thinny")
    monkeypatch.setattr(host, "LIB", write(tmp_path / "checkout/thinny/cli.py").parent.parent)  # this code
    monkeypatch.setattr(update, "SELF_UPDATE_LOCK", write(tmp_path / "run/thinny-self-update.lock"))
    monkeypatch.setattr(system, "LOCK_FILE", write(tmp_path / "run/thinny.lock"))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.sys, "stdin", type("Terminal", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(uninstall.socket, "gethostname", lambda: "office-server")
    monkeypatch.setattr("builtins.input", srv.input)
    return srv


def test_uninstall_deletes_every_instance_and_everything_thinny_set_up(server, capsys):
    srv = server
    assert cli.main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert all(f"{name} ({state})" in out for name, state in
               (("alice", "active"), ("bob", "disabled"), ("carol", "pending"), ("dave", "pending")))
    assert srv.calls[:3] == ["systemctl disable --now thinny-update.timer", "systemctl stop thinny-update.service",
                            "systemctl disable --now thinny-trace.service"]
    assert [c for c in srv.calls if c.startswith("incus delete")] == \
        ["incus delete --force alice", "incus delete --force bob", "incus delete --force carol"]
    assert srv.index("incus delete --force carol") < srv.index("incus profile delete thinny-instance") \
        < srv.index("incus image delete f00d") < srv.index("incus storage delete default")
    assert not srv.containers and not srv.profile and not srv.pool and not srv.images and not srv.table
    assert not srv.served
    assert "systemctl disable --now caddy" in srv.calls
    # incus.service no longer loads the table when it goes
    reload = srv.index("systemctl daemon-reload")
    assert host.SYSTEMD / "incus.service.d/thinny.conf" not in srv.files[reload]
    assert reload < srv.index("nft delete table inet thinny")
    assert thinny_files() == []
    assert all(uninstall.LIB in files and uninstall.COMMAND in files for files in srv.files)  # a rerun can continue
    assert (host.LIB / "thinny/cli.py").exists()
    assert "apt purge" in out and "until the next reboot" in out


def test_tailscale_stops_serving_only_the_instances_ports(server):
    srv = server
    srv.served = {443, 9121, 9123, 9150}   # bob's (disabled), dave's (pending) and two of the admin's
    assert cli.main(["uninstall"]) == 0
    assert srv.served == {443, 9150}


def test_an_install_that_stopped_before_setup_is_removed(server):
    srv = server
    for path in (settings.SITE_TOML, system.STATE_DIR / "instances.json", host.CADDYFILE,
                 *(host.SYSTEMD / unit for unit in UNITS)):
        path.unlink()
    srv.containers, srv.served, srv.table, srv.profile, srv.pool = {}, set(), False, False, False
    assert cli.main(["uninstall"]) == 0
    assert srv.calls == ["tailscale serve status --json", "incus profile list --format csv --columns n",
                         "incus storage list --format csv --columns n", "systemctl daemon-reload", "nft list tables"]
    assert thinny_files() == []


def test_what_thinny_did_not_set_up_stays(server):
    srv = server
    host.CADDYFILE.write_text("thinny.example.com {\n\treverse_proxy 127.0.0.1:3000\n}\n")
    write(host.SYSTEMD / "incus.service.d/admin.conf")
    srv.tailscale = None
    assert cli.main(["uninstall"]) == 0
    assert host.CADDYFILE.exists() and (host.SYSTEMD / "incus.service.d/admin.conf").exists()
    assert not any("caddy" in c or c.startswith("tailscale") for c in srv.calls)


def test_a_storage_pool_something_else_uses_is_kept_and_reported(server, capsys):
    srv = server
    srv.images.append({"fingerprint": "beef", "cached": False})   # the admin's own image
    assert cli.main(["uninstall"]) == 0
    assert srv.pool and srv.images == [{"fingerprint": "beef", "cached": False}]
    assert "incus storage volume list default" in capsys.readouterr().out
    assert thinny_files() == []


def test_a_clone_in_another_place_is_kept(server, tmp_path, capsys):
    clone = write(tmp_path / "home/admin/thinny/install.sh").parent
    (uninstall.LIB / "source").write_text(f"{clone}\n")
    assert cli.main(["uninstall"]) == 0
    assert clone.exists() and uninstall.CLONE.exists()
    assert str(clone) in capsys.readouterr().out


@pytest.mark.parametrize("terminal, answers", [(False, ["office-server"]), (True, ["yes"]), (True, [""]), (True, [])])
def test_nothing_is_removed_without_the_hostname_typed_on_a_terminal(server, monkeypatch, capsys, terminal, answers):
    srv = server
    if not terminal:
        monkeypatch.setattr(cli.sys, "stdin", type("Pipe", (), {"isatty": lambda self: False})())
    srv.answers = answers
    files = thinny_files()
    assert cli.main(["uninstall"]) == 1
    assert "nothing removed" in capsys.readouterr().err
    assert srv.calls == []
    assert thinny_files() == files
    assert srv.answers == answers * (not terminal)   # not even asked


@pytest.mark.parametrize("lock", ["self-update", "thinny"])
def test_nothing_is_removed_while_another_thinny_runs(server, lock):
    srv = server
    with system.lock(update.SELF_UPDATE_LOCK if lock == "self-update" else None):
        assert cli.main(["uninstall"]) == 1
    assert srv.calls == [] and srv.answers == ["office-server"]


@pytest.mark.parametrize("failing, answer", [
    ("systemctl disable --now thinny-update.timer", ("", 1)),
    ("tailscale serve status --json", ("", 1)),
    ("tailscale serve status --json", ("<html>", 0)),           # not JSON
    ("incus profile list", ("", 1)),
    ("incus profile delete thinny-instance", ("", 1)),         # e.g. a container of the admin's uses it
    ("incus storage list", ("", 1)),
    ("nft list tables", ("", 1)),
    ("nft delete table inet thinny", ("", 1)),
])
def test_a_rerun_continues_where_a_failed_uninstall_stopped(server, capsys, failing, answer):
    srv = server
    srv.fails = {failing: answer}
    assert cli.main(["uninstall"]) == 1
    assert failing in capsys.readouterr().err
    assert {settings.SITE_TOML.parent, system.STATE_DIR, uninstall.LIB, uninstall.COMMAND} <= set(thinny_files())

    srv.fails, srv.answers = {}, ["office-server"]
    assert cli.main(["uninstall"]) == 0
    assert len([c for c in srv.calls if c.startswith("incus delete")]) == 3
    assert thinny_files() == []
