import datetime as dt
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from test_instances import incus, server_disk  # noqa: F401 - incus: the fake Incus fixture

from thinny import backup, checks, export, host, render, settings, update
from thinny.registry import Instance, Registry
from thinny.settings import Settings
from thinny.system import CommandError

S = Settings(backup_target="you@nas:thinny-backups")


class Machine:
    """The backup machine, whose shell commands run in a home of their own with a few tools, and the client side of
    borg on its repositories. Records every command thinny runs as text."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "nas-home"
        self.bin = tmp_path / "nas-bin"
        self.home.mkdir()
        self.bin.mkdir()
        for tool in ("sh", "cat", "mkdir", "tail", "grep"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.tool("borg", '[ "$1 $2" = "key export" ] && printf "BORG_KEY %s\\n" "$3" > "$4"')
        # Like the real one, it reads all of a new crontab before writing it.
        self.tool("crontab", 'f="$HOME/.crontab"\ncase "$1" in -l) cat "$f" 2>/dev/null || exit 1 ;; '
                             '-) t=$(cat); printf "%s\\n" "$t" > "$f" ;; esac')
        self.calls = []
        self.logins = []
        self.passphrases = {}   # repository folder -> its passphrase
        self.unrestricted = False   # whether thinny's key gets a shell there
        self.archives = {}   # name -> {"time", "comment", "data" (bytes) or "files" (path -> bytes)}, in order
        self.down = False   # whether the backup machine answers
        self.server = None   # the fake Incus, for the commands thinny runs on the server
        self.sizes = {}   # container -> what du says its folder takes

    def tool(self, name, script):
        (self.bin / name).write_text(f"#!/bin/sh\n{script}\n")
        (self.bin / name).chmod(0o755)

    def run(self, cmd, *, check=True, input=None, timeout=600, env=None, cwd=None):
        self.calls.append(" ".join(cmd))
        out, err, code = self.answer(cmd, env or {})
        if check and code:
            raise CommandError(f"{' '.join(cmd)} -> exit {code}\n{out}{err}")
        return subprocess.CompletedProcess(cmd, code, out, err)

    def answer(self, cmd, env):
        if cmd[0] == "ssh-keygen":
            key = Path(cmd[cmd.index("-f") + 1])
            key.write_text("PRIVATE\n")
            comment = cmd[cmd.index("-C") + 1]   # a key of its own for each server
            Path(f"{key}.pub").write_text(f"ssh-ed25519 AAAA{comment.partition('@')[2]} {comment}\n")
        elif cmd[0] == "ssh" and "-O" in cmd:   # closing the admin's login
            pass
        elif cmd[0] == "ssh" and "-i" in cmd:   # thinny's own key, which runs borg serve there
            return ("thinny-unrestricted\n" if self.unrestricted else ""), "", 0
        elif cmd[0] == "ssh":   # through the admin's login
            res = subprocess.run(["/bin/sh", "-c", cmd[-1]], capture_output=True, text=True, cwd=self.home,
                                 env={"HOME": str(self.home), "PATH": str(self.bin)})
            return res.stdout, res.stderr, res.returncode
        elif cmd[0] == "borg":
            if self.down:
                return "", "Connection closed by remote host\n", 2
            return self.borg(cmd[1:], env)
        elif cmd[0] == "du":
            return f"{self.sizes.get(Path(cmd[-1]).name, 2**20)}\t{cmd[-1]}\n", "", 0
        elif self.server:
            res = self.server.run(cmd, check=False)
            return res.stdout, res.stderr, res.returncode
        return "", "", 0

    def repo(self, env):
        return self.home / env["BORG_REPO"].partition(":")[2]

    def borg(self, args, env):
        repo = self.repo(env)
        assert args[1:3] == ["--lock-wait", "600"]   # for the monthly cleanup's lock
        command, args = args[0], args[3:]

        def option(name):
            return args[args.index(name) + 1] if name in args else None
        if command == "create" and "--content-from-command" in args:   # ::NAME -- COMMAND
            res = self.server.run(args[args.index("--") + 1:], check=False)
            if res.returncode:
                return "", f"Command {args[args.index('--') + 1]!r} exited with {res.returncode}\n", 2
            name = next(a for a in args if a.startswith("::"))[2:]
            self.archives[name] = {"time": option("--timestamp"), "comment": option("--comment"),
                                   "data": res.stdout.encode()}
        elif command == "create":   # ::NAME PATH...
            at = next(i for i, a in enumerate(args) if a.startswith("::"))
            excluded = [a.removeprefix("--exclude=pp:") for a in args if a.startswith("--exclude=pp:")]
            files = {}
            for top in args[at + 1:]:
                for folder, _, names in os.walk(top):
                    for name in names:
                        path = os.path.join(folder, name)
                        if not any(path == e or path.startswith(e + "/") for e in excluded):
                            files[path.lstrip("/")] = Path(path).read_bytes()
            self.archives[args[at][2:]] = {"time": option("--timestamp"), "comment": "", "files": files}
        elif command == "list":
            return json.dumps({"archives": [{"name": name} for name in self.archives]}), "", 0
        elif command == "delete":
            for name in fnmatch.filter(list(self.archives), option("--glob-archives")):
                del self.archives[name]
        elif command == "init":
            repo.mkdir()
            (repo / "config").write_text("[repository]\n")
            self.passphrases[repo] = env["BORG_PASSPHRASE"]
        elif command == "info" and self.passphrases.get(repo) != env["BORG_PASSPHRASE"]:
            return "", "passphrase supplied in BORG_PASSPHRASE, by BORG_PASSCOMMAND or via BORG_PASSPHRASE_FD is " \
                       "incorrect.\n", 52
        return "", "", 0

    def ran(self, text):
        return any(text in call for call in self.calls)

    def file(self, path):
        return (self.home / path).read_text()


@pytest.fixture
def nas(tmp_path, monkeypatch):
    machine = Machine(tmp_path)
    monkeypatch.setattr(backup, "run", machine.run)
    monkeypatch.setattr(backup, "login", lambda account, master: machine.logins.append(account))
    monkeypatch.setattr(backup, "KEY", tmp_path / "etc" / "backup-key")
    monkeypatch.setattr(backup, "KNOWN_HOSTS", tmp_path / "etc" / "backup-known-hosts")
    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(backup.socket, "gethostname", lambda: "thinny")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    (tmp_path / "etc").mkdir()
    return machine


def restricted_line(nas, server="thinny"):
    repo = nas.home / "thinny-backups"
    return f'command="{nas.bin}/borg serve --append-only --restrict-to-repository {repo}",restrict ' \
           f"ssh-ed25519 AAAA{server} thinny-backup@{server}"


def test_init_sets_up_the_backup_machine_and_shows_the_new_passphrase_once(nas, capsys):
    (nas.home / ".ssh").mkdir(mode=0o700)
    (nas.home / ".ssh/authorized_keys").write_text("ssh-ed25519 AAAAyours you@laptop")   # without a newline at its end
    (nas.home / ".crontab").write_text("5 4 * * * your-own-job\n")
    backup.init(S)

    assert nas.logins == ["you@nas"]   # once, as the account in target
    # thinny's own key, which may only add backups to this repository, beside the admin's keys
    assert nas.file(".ssh/authorized_keys") == f"ssh-ed25519 AAAAyours you@laptop\n{restricted_line(nas)}\n"
    assert backup.KEY.exists()
    # a repository, encrypted with a new passphrase that secrets.env keeps and the admin sees only now
    passphrase = settings.secret("THINNY_BACKUP_PASSPHRASE")
    assert len(passphrase) >= 32 and nas.passphrases[nas.home / "thinny-backups"] == passphrase
    assert nas.ran("borg init --lock-wait 600 --encryption repokey-blake2")
    assert passphrase in capsys.readouterr().out
    assert settings.SECRETS_ENV.stat().st_mode & 0o777 == 0o600
    # a copy of its key beside it, and a monthly cleanup there, beside the admin's jobs
    assert nas.file("thinny-backups.key-copy") == f"BORG_KEY {nas.home}/thinny-backups\n"
    repo = nas.home / "thinny-backups"
    assert nas.file(".crontab") == (
        f"5 4 * * * your-own-job\n0 12 1 * * {nas.bin}/borg check --repository-only --lock-wait 600 {repo} && "
        f"{nas.bin}/borg compact --lock-wait 600 {repo}\n")
    assert nas.ran("-O exit you@nas")   # the admin's login is closed


def test_init_again_changes_nothing_and_shows_no_passphrase(nas, capsys):
    backup.init(S)
    passphrase = settings.secret("THINNY_BACKUP_PASSPHRASE")
    files = {name: nas.file(name) for name in (".ssh/authorized_keys", ".crontab", "thinny-backups.key-copy")}
    capsys.readouterr()
    nas.calls.clear()

    backup.init(S)
    assert {name: nas.file(name) for name in files} == files
    assert settings.secret("THINNY_BACKUP_PASSPHRASE") == passphrase
    assert not nas.ran("borg init") and not nas.ran("ssh-keygen")
    assert passphrase not in capsys.readouterr().out


def test_a_new_server_joins_the_backups_with_the_passphrase_the_admin_kept(nas, monkeypatch, capsys):
    backup.init(S)
    passphrase = settings.secret("THINNY_BACKUP_PASSPHRASE")
    (nas.home / "thinny-backups.key-copy").write_text("the key as it was")
    # another server, without the old one's key and passphrase
    settings.SECRETS_ENV.unlink()
    backup.KEY.unlink()
    monkeypatch.setattr(backup.socket, "gethostname", lambda: "new-server")
    typed = iter(["wrong", passphrase])
    monkeypatch.setattr(backup.getpass, "getpass", lambda prompt: next(typed))
    capsys.readouterr()
    nas.calls.clear()

    backup.init(S)
    assert settings.secret("THINNY_BACKUP_PASSPHRASE") == passphrase
    assert "That passphrase does not open the backups." in capsys.readouterr().out
    assert nas.file(".ssh/authorized_keys") == f"{restricted_line(nas)}\n{restricted_line(nas, 'new-server')}\n"
    # neither a new repository nor a new copy of its key, which may be a broken-in server's by now
    assert not nas.ran("borg init")
    assert nas.file("thinny-backups.key-copy") == "the key as it was"


def test_init_refuses_thinnys_key_listed_there_with_other_options(nas):
    backup.init(S)
    lines = nas.file(".ssh/authorized_keys").replace("--append-only ", "")
    (nas.home / ".ssh/authorized_keys").write_text(lines)
    with pytest.raises(CommandError, match="with other options"):
        backup.init(S)
    assert nas.file(".ssh/authorized_keys") == lines


def test_init_refuses_a_key_that_gets_a_shell_there(nas):
    nas.unrestricted = True
    with pytest.raises(CommandError, match="runs any command"):
        backup.init(S)
    assert not nas.ran("borg init")


def test_init_asks_for_borg_on_the_backup_machine(nas):
    (nas.bin / "borg").unlink()
    with pytest.raises(CommandError, match="borg is not installed on nas: install it there"):
        backup.init(S)
    assert not (nas.home / ".ssh").exists()


def test_without_a_crontab_there_init_shows_the_cleanup_to_schedule(nas, capsys):
    (nas.bin / "crontab").unlink()
    backup.init(S)
    repo = nas.home / "thinny-backups"
    assert f"{nas.bin}/borg check --repository-only --lock-wait 600 {repo} && " \
           f"{nas.bin}/borg compact --lock-wait 600 {repo}" in capsys.readouterr().out


def test_init_needs_the_target():
    with pytest.raises(CommandError, match=r"set \[backup\] target"):
        backup.init(Settings())


@pytest.mark.parametrize("value", ["nas:backups", "you@nas", "you@nas:back ups", "you@nas:$(reboot)", "You@nas:x"])
def test_the_target_is_a_plain_user_host_and_path(value):
    with pytest.raises(settings.SettingsError, match="backup.target must be user@host:path"):
        settings.parse(f'[access]\ndefault = "local"\n[backup]\ntarget = "{value}"\n')


class Clock(dt.datetime):
    """Each backup a night later: 03:00 local time from 2026-11-19 on."""
    days = 0

    @classmethod
    def now(cls, tz=None):
        Clock.days += 1
        at = dt.datetime(2026, 11, 18, 3) + dt.timedelta(days=Clock.days)
        return at.astimezone(tz) if tz else at


@pytest.fixture
def server(nas, incus, tmp_path, monkeypatch):
    """This server with backups set up: alice and bob run, carol is disabled, dave is removed; its settings and state
    in tmp_path."""
    nas.server = incus
    backup.KEY.write_text("PRIVATE\n")
    backup.KNOWN_HOSTS.write_text("nas ssh-ed25519 AAAAnas\n")
    monkeypatch.setattr(settings, "CONFIG_TOML", tmp_path / "etc" / "config.toml")
    monkeypatch.setattr(settings, "SECRETS_ENV", tmp_path / "etc" / "secrets.env")
    monkeypatch.setattr(backup, "dt", types.SimpleNamespace(datetime=Clock, UTC=dt.UTC))
    Clock.days = 0
    settings.CONFIG_TOML.write_text('[backup]\ntarget = "you@nas:thinny-backups"\n')
    settings.SECRETS_ENV.write_text("THINNY_BACKUP_PASSPHRASE=kept-in-a-password-manager\n")
    state = tmp_path / "state"
    for module in (backup, update, export):
        monkeypatch.setattr(module, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(backup, "STATUS_FILE", state / "backup-status.json")
    monkeypatch.setattr(update, "UNFINISHED", state / "update-unfinished")
    monkeypatch.setattr(export, "EXPORTS", state / "exports")
    monkeypatch.setattr(host, "caddy_root_ca", lambda: tmp_path / "caddy" / "local" / "root.crt")
    (state / "exports").mkdir(parents=True)
    (state / "exports" / "alice-2026.zip").write_text("alice's chats, for her users")
    (state / "instances.json").write_text('{"instances": {}}\n')
    reg = Registry({name: Instance(name, 9120 + i, "local", state=state_, built_ins=render.BUILT_INS)
                    for i, (name, state_) in enumerate([("alice", "active"), ("bob", "active"), ("carol", "disabled"),
                                                        ("dave", "removed")])})
    for name, running in (("alice", True), ("bob", True), ("carol", False)):
        incus.containers[name] = {"state": "RUNNING" if running else "STOPPED", "devices": {}, "account": True,
                                  "hermes": True, "unit": True,
                                  "files": {"/home/x/.hermes/state.db": f"{name}'s chats"}}
        incus.pools[f"thinny-{name}"] = {"size": 30 * 2**30, "used": 0}
    return reg


def runs(nas):
    """The runs the backups have archives of, complete or not."""
    return sorted({name.split(".")[0] for name in nas.archives if not name.startswith("before-restore.")})


def test_a_backup_takes_every_container_running_and_then_the_servers_settings(server, nas, incus, tmp_path):
    (tmp_path / "caddy" / "local").mkdir(parents=True)
    (tmp_path / "caddy" / "local" / "root.key").write_text("the office's certificate authority")
    assert backup.create(S, server) == 0

    [run] = runs(nas)
    assert run == "2026-11-19T03:00:00"
    # disabled carol too, not removed dave; none stopped
    assert list(nas.archives) == [f"{run}.instance.alice", f"{run}.instance.bob", f"{run}.instance.carol",
                                  f"{run}.server"]
    assert not incus.ran("incus stop") and incus.containers["alice"]["state"] == "RUNNING"
    alice = nas.archives[f"{run}.instance.alice"]
    assert json.loads(alice["data"])["files"]["/home/x/.hermes/state.db"] == "alice's chats"
    assert incus.ran("incus export alice - --instance-only --compression none --quiet")   # so borg stores only changes
    # what a restore needs to know
    assert json.loads(alice["comment"]) == {"release": ["v2", "c0ffee"], "built_ins": render.BUILT_INS, "disk_gb": 30}
    # one time for the whole run, so that every instance's backups and the server's are kept alike
    assert {a["time"] for a in nas.archives.values()} == \
        {dt.datetime.fromisoformat(run).astimezone(dt.UTC).replace(tzinfo=None).isoformat()}
    # the server's settings, tokens, instances and certificate authority; not exports nor its key there
    files = nas.archives[f"{run}.server"]["files"]
    assert files[str(settings.CONFIG_TOML).lstrip("/")].startswith(b"[backup]")
    assert files[str(settings.SECRETS_ENV).lstrip("/")] == b"THINNY_BACKUP_PASSPHRASE=kept-in-a-password-manager\n"
    assert str(tmp_path / "state" / "instances.json").lstrip("/") in files
    assert files[str(tmp_path / "caddy/local/root.key").lstrip("/")] == b"the office's certificate authority"
    assert not any("exports" in path or "backup-key" in path or "known-hosts" in path for path in files)

    status = json.loads(backup.STATUS_FILE.read_text())
    assert (status["result"], status["time"], status["last_success"]) == ("ok", run, run)
    assert checks.backup_status(dt.datetime.fromisoformat(run).astimezone(dt.UTC))[0] == "OK"


def test_a_failed_backup_keeps_no_part_of_it_and_holds_the_nightly_update(server, nas, incus):
    assert backup.create(S, server) == 0
    [good] = runs(nas)
    incus.containers["bob"]["export_fails"] = True
    assert backup.create(S, server, scheduled=True) == 1

    assert runs(nas) == [good]   # alice's archive of the failed run is gone
    status = json.loads(backup.STATUS_FILE.read_text())
    assert (status["result"], status["last_success"]) == ("FAILED", good) and "incus" in status["detail"]
    level, message, _ = checks.backup_status(dt.datetime.now(dt.UTC))
    assert level == "FAIL" and "the nightly update waits" in message
    assert "ExecStartPre=/usr/local/sbin/thinny backup --scheduled\nExecStart=/usr/local/sbin/thinny update" \
        in render.UPDATE_SERVICE


def test_a_run_cut_short_is_deleted_by_the_next_and_old_backups_go(server, nas):
    nas.archives["2026-11-01T03:00:00.instance.alice"] = {"data": b"{}"}   # its server's archive never came
    nas.archives["before-restore.alice.2026-11-02T10:00:00"] = {"data": b"{}"}
    assert backup.create(S, server) == 0
    [run] = runs(nas)
    assert sorted(nas.archives) == sorted([f"{run}.instance.alice", f"{run}.instance.bob", f"{run}.instance.carol",
                                           f"{run}.server", "before-restore.alice.2026-11-02T10:00:00"])
    keep = "--keep-daily 7 --keep-weekly 4 --keep-monthly 6"
    for name in ("alice", "bob", "carol"):   # each on its own, so that a new one does not count against the others
        assert nas.ran(f"borg prune --lock-wait 600 {keep} --glob-archives *.instance.{name}")
    assert nas.ran(f"borg prune --lock-wait 600 {keep} --glob-archives ????-??-??T??:??:??.server")
    assert nas.ran("borg prune --lock-wait 600 --keep-within 30d --glob-archives before-restore.*")
    assert nas.ran("borg compact")


def test_an_instance_named_server_is_kept_apart_from_the_servers_own_backups(server, nas, incus, capsys):
    server.instances["server"] = server.instances.pop("bob")
    server.get("server").name = "server"
    incus.containers["server"] = incus.containers.pop("bob")
    incus.pools["thinny-server"] = incus.pools.pop("thinny-bob")
    assert backup.create(S, server) == 0
    run = runs(nas)[0]
    assert f"{run}.instance.server" in nas.archives and f"{run}.server" in nas.archives
    assert nas.ran("--glob-archives *.instance.server")
    capsys.readouterr()
    backup.list_backups(S)
    assert capsys.readouterr().out == f"{run}  alice, carol, server\n"


def test_a_container_without_room_on_the_servers_disk_for_its_archive_is_not_exported(server, nas, incus, monkeypatch):
    server_disk(monkeypatch, 100 * 2**30, free=15 * 2**30)   # 5 GiB above the last tenth
    nas.sizes["bob"] = 6 * 2**30
    assert backup.create(S, server) == 1
    assert "the server needs 6.0 GiB free on top of 10% of its disk to back up bob" in \
        json.loads(backup.STATUS_FILE.read_text())["detail"]
    assert not incus.ran("incus export bob") and not nas.archives


def test_during_an_unfinished_update_a_backup_records_the_release_it_goes_to(server, nas):
    update.UNFINISHED.write_text("v3 c3 bob\n")
    backup.create(S, server)
    assert {json.loads(a["comment"])["release"][0] for a in nas.archives.values() if a.get("comment")} == {"v3"}


def test_without_backups_the_nightly_update_goes_on(nas):
    assert backup.create(Settings(), Registry(), scheduled=True) == 0
    assert not nas.calls
    with pytest.raises(CommandError, match="backups are off"):
        backup.create(Settings(), Registry())


def test_the_list_shows_complete_runs_and_states_before_a_restore(server, nas, capsys):
    backup.create(S, server)
    [run] = runs(nas)
    nas.archives["before-restore.bob.2026-11-02T10:00:00"] = {"data": b"{}"}
    capsys.readouterr()
    backup.list_backups(S)
    assert capsys.readouterr().out == f"{run}  alice, bob, carol\n" \
                                      "before-restore.bob.2026-11-02T10:00:00  bob as it was before a restore\n"


@pytest.mark.parametrize("status, hours, level, text", [
    (None, 0, "WARN", "no backup yet"),
    ({"result": "ok", "detail": "3 instances and the server"}, 2, "OK", "3 instances and the server"),
    ({"result": "ok", "detail": "3 instances and the server"}, 40, "FAIL", "over 36 h ago"),
    ({"result": "FAILED", "detail": "Connection closed"}, 2, "FAIL", "the nightly update waits"),
])
def test_doctor_tells_when_backups_stop_working(server, status, hours, level, text):
    if status:
        when = (dt.datetime.now() - dt.timedelta(hours=hours)).replace(microsecond=0).isoformat()
        backup.STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        backup.STATUS_FILE.write_text(json.dumps(status | {"time": when, "last_success": when}))
    result = checks.backup_status(dt.datetime.now(dt.UTC))
    assert result[0] == level and text in result[1]


def test_doctor_asks_for_init_when_backups_are_on_but_not_set_up(nas):
    level, message, fix = next(checks.backups(S, Registry()))
    assert (level, fix) == ("FAIL", "thinny backup init")
    assert not list(checks.backups(Settings(), Registry()))
