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

from thinny import backup, checks, export, host, render, settings, trace, update
from thinny.registry import Instance, Registry
from thinny.settings import Settings
from thinny.system import CommandError

S = Settings(backup_target="you@nas:thinny-backups")
REGISTRY_LOAD, REGISTRY_SAVE = Registry.load.__func__, Registry.save   # before the incus fixture stubs saving


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
        self.applied = []   # thinny config apply's runs on the server

    def tool(self, name, script):
        (self.bin / name).write_text(f"#!/bin/sh\n{script}\n")
        (self.bin / name).chmod(0o755)

    def run(self, cmd, *, check=True, input=None, timeout=600, env=None, cwd=None):
        self.calls.append(" ".join(cmd))
        out, err, code = self.answer(cmd, env or {}, cwd)
        if check and code:
            raise CommandError(f"{' '.join(cmd)} -> exit {code}\n{out}{err}")
        return subprocess.CompletedProcess(cmd, code, out, err)

    def answer(self, cmd, env, cwd=None):
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
            return self.borg(cmd[1:], env, cwd)
        elif cmd[0] == "bash":   # borg extract --stdout ::ARCHIVE | incus import - NAME --storage POOL
            if self.down:
                return "", "Connection closed by remote host\n", 2
            res = self.server.run(["incus", "import", "-", cmd[-2], "--storage", cmd[-1], "--quiet"], check=False,
                                  input=self.archives[cmd[-3]]["data"].decode())
            return res.stdout, res.stderr, res.returncode
        elif cmd[0] == "du":
            return f"{self.sizes.get(Path(cmd[-1]).name, 2**20)}\t{cmd[-1]}\n", "", 0
        elif self.server:
            res = self.server.run(cmd, check=False)
            return res.stdout, res.stderr, res.returncode
        return "", "", 0

    def repo(self, env):
        return self.home / env["BORG_REPO"].partition(":")[2]

    def borg(self, args, env, cwd=None):
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
            try:
                comment = option("--comment").format()   # borg's {placeholders}, of which the test has none
            except (KeyError, IndexError, ValueError):
                return "", f"Invalid placeholder in string: {option('--comment')}\n", 2
            self.archives[name] = {"time": option("--timestamp"), "comment": comment, "data": res.stdout.encode()}
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
        elif command == "extract":   # ::NAME, its files under cwd
            for path, data in self.archives[args[-1][2:]]["files"].items():
                (Path(cwd) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(cwd) / path).write_bytes(data)
        elif command == "info" and "--json" in args:   # of ::ARCHIVE
            found = self.archives[args[-1][2:]]
            return json.dumps({"archives": [{"comment": found["comment"],
                                             "stats": {"original_size": len(found["data"])}}]}), "", 0
        elif command == "delete":
            for name in fnmatch.filter(list(self.archives), option("--glob-archives")):
                del self.archives[name]
        elif command == "init":
            repo.mkdir()
            (repo / "config").write_text("[repository]\n")
            self.passphrases[repo] = env["BORG_PASSPHRASE"]
        elif command == "info" and self.passphrases.get(repo) != env["BORG_PASSPHRASE"]:
            return "", "Passphrase supplied in BORG_PASSPHRASE, by BORG_PASSCOMMAND, or via BORG_PASSPHRASE_FD is " \
                       "incorrect.\n", 2
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


def test_the_new_passphrase_shows_once_the_backups_exist_even_if_init_fails_after(nas, capsys):
    nas.tool("crontab", "exit 1")
    with pytest.raises(CommandError, match="crontab"):
        backup.init(S)
    passphrase = settings.secret("THINNY_BACKUP_PASSPHRASE")
    assert passphrase in capsys.readouterr().out
    nas.tool("crontab", 'f="$HOME/.crontab"\ncase "$1" in -l) cat "$f" 2>/dev/null || exit 1 ;; '
                        '-) t=$(cat); printf "%s\\n" "$t" > "$f" ;; esac')
    backup.init(S)   # the one it showed
    assert settings.secret("THINNY_BACKUP_PASSPHRASE") == passphrase and nas.file(".crontab")


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


CONFIG = '[access]\ndefault = "local"\n[backup]\ntarget = "you@nas:thinny-backups"\n'


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
    settings.CONFIG_TOML.write_text(CONFIG)
    settings.SECRETS_ENV.write_text("THINNY_BACKUP_PASSPHRASE=kept-in-a-password-manager\n")
    state = tmp_path / "state"
    for module in (backup, update, export):
        monkeypatch.setattr(module, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(backup, "STATUS_FILE", state / "backup-status.json")
    monkeypatch.setattr(update, "UNFINISHED", state / "update-unfinished")
    monkeypatch.setattr(export, "EXPORTS", state / "exports")
    ca = tmp_path / "caddy/.local/share/caddy/pki/authorities/local"   # as deep as Caddy's
    monkeypatch.setattr(host, "caddy_root_ca", lambda: ca / "root.crt")
    (state / "exports").mkdir(parents=True)
    (state / "exports" / "alice-2026.zip").write_text("alice's chats, for her users")
    (state / "instances.json").write_text('{"instances": {}}\n')
    monkeypatch.setattr(backup, "RESTORE", state / "restore.json")
    for module, name in ((host, "RELEASE_FILE"), (host, "WEBUI_RELEASE_FILE"), (trace, "DIR")):
        monkeypatch.setattr(module, name, state / getattr(module, name).name)
    host.write_release("v2", "c0ffee")
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: REGISTRY_LOAD(cls, path or
                                                                                             state / "instances.json")))
    monkeypatch.setattr(Registry, "save", lambda self, path=None: REGISTRY_SAVE(self, path or state / "instances.json"))
    monkeypatch.setattr(backup, "NETWORK_INTERFACES", tmp_path / "net")
    (tmp_path / "net" / "eth0").mkdir(parents=True)
    # with the containers there are by then
    monkeypatch.setattr(backup.apply, "apply", lambda s: nas.applied.append((s, sorted(incus.containers))))
    monkeypatch.setattr(host, "tailscale_status", lambda: None)
    monkeypatch.setattr(host, "FILES", tmp_path / "files")
    (tmp_path / "files" / "model-guard").mkdir(parents=True)
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
    host.caddy_root_ca().parent.mkdir(parents=True)
    (host.caddy_root_ca().parent / "root.key").write_text("the office's certificate authority")
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
    assert files[str(settings.CONFIG_TOML).lstrip("/")] == CONFIG.encode()
    assert files[str(settings.SECRETS_ENV).lstrip("/")] == b"THINNY_BACKUP_PASSPHRASE=kept-in-a-password-manager\n"
    assert str(tmp_path / "state" / "instances.json").lstrip("/") in files
    assert files[str(host.caddy_root_ca().parent / "root.key").lstrip("/")] == b"the office's certificate authority"
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
    # the server's first: cut short after them, what is left of a run counts as incomplete
    assert nas.calls.index(f"borg prune --lock-wait 600 {keep} --glob-archives ????-??-??T??:??:??.server") < \
        nas.calls.index(f"borg prune --lock-wait 600 {keep} --glob-archives *.instance.alice")
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


def test_an_instance_without_its_container_fails_the_backup(server, nas, incus):
    del incus.containers["carol"]   # disabled, so it keeps its data in it
    assert backup.create(S, server) == 1
    assert json.loads(backup.STATUS_FILE.read_text())["detail"] == "no container for carol (see thinny doctor)"
    assert not nas.archives


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


def chats(incus, name):
    return incus.containers[name]["files"]["/home/x/.hermes/state.db"]


def test_an_instance_comes_back_as_it_was_and_its_state_just_before_is_kept(server, nas, incus, capsys):
    backup.create(S, server)
    [run] = runs(nas)
    incus.containers["alice"]["files"]["/home/x/.hermes/state.db"] = "alice's chats, some deleted by mistake"
    incus.calls.clear()
    capsys.readouterr()

    backup.restore(S, server, "alice")
    assert chats(incus, "alice") == "alice's chats"
    # stopped, its state then kept, and only then its container replaced, in its own pool
    fallback = next(name for name in nas.archives if name.startswith("before-restore.alice."))
    assert json.loads(nas.archives[fallback]["data"])["files"]["/home/x/.hermes/state.db"] == \
        "alice's chats, some deleted by mistake"
    assert json.loads(nas.archives[fallback]["data"])["state"] == "STOPPED"
    assert incus.index("incus stop alice") < incus.index("incus delete --force alice") < \
        incus.index("incus import - alice --storage thinny-alice")
    assert "thinny-alice" in incus.pools
    # finished as a moved instance is, at this server's release, with its users' settings
    alice = server.get("alice")
    assert (alice.state, incus.containers["alice"]["state"]) == ("active", "RUNNING")
    assert incus.ran("bash /tmp/hermes-install.sh --channel stable --commit c0ffee --non-interactive")
    assert not incus.ran("display.language")
    assert not backup.RESTORE.exists()
    assert f"alice is back as it was in {run}.instance.alice. As it was just before, it is in the backup {fallback}: " \
           f"thinny backup restore alice --from {fallback}" in capsys.readouterr().out
    assert chats(incus, "bob") == "bob's chats"   # nothing else changes

    # and back to how it was before
    backup.restore(S, server, "alice", fallback)
    assert chats(incus, "alice") == "alice's chats, some deleted by mistake"


def test_a_restore_takes_the_run_named(server, nas, incus):
    backup.create(S, server)
    [first] = runs(nas)
    incus.containers["alice"]["files"]["/home/x/.hermes/state.db"] = "alice's chats of the next day"
    backup.create(S, server)
    backup.restore(S, server, "alice", first)
    assert chats(incus, "alice") == "alice's chats"
    with pytest.raises(CommandError, match="no backup 2026-11-01T03:00:00 of alice"):
        backup.restore(S, server, "alice", "2026-11-01T03:00:00")
    with pytest.raises(CommandError, match="no backup of dave"):
        backup.restore(S, Registry({"dave": Instance("dave", 9130, "local", state="active")}), "dave")


@pytest.mark.parametrize("release, refused", [(["v2", "c0ffee"], False), (["v1", "c1"], False),
                                              (["v3", "c3"], True), (["", ""], True), (["nightly", "c4"], True)])
def test_a_backup_of_a_newer_release_is_refused_before_anything_changes(server, nas, incus, release, refused):
    backup.create(S, server)
    [run] = runs(nas)
    archive = nas.archives[f"{run}.instance.alice"]
    archive["comment"] = json.dumps(json.loads(archive["comment"]) | {"release": release})
    incus.calls.clear()
    if refused:
        with pytest.raises(CommandError, match="Hermes databases cannot be downgraded"):
            backup.restore(S, server, "alice")
        assert not incus.ran("incus stop") and not backup.RESTORE.exists()
    else:   # an older one is brought to this server's
        backup.restore(S, server, "alice")
        assert incus.ran("--commit c0ffee")


@pytest.mark.parametrize("why, error", [("update", "an update is unfinished"), ("disabled", "carol is disabled"),
                                        ("room", "the server needs")])
def test_a_restore_that_cannot_work_changes_nothing(server, nas, incus, monkeypatch, why, error):
    backup.create(S, server)
    name = "carol" if why == "disabled" else "alice"
    if why == "update":
        update.UNFINISHED.write_text("v3 c3 bob\n")
    if why == "room":
        server_disk(monkeypatch, 100 * 2**30, free=10 * 2**30)
    incus.calls.clear()
    with pytest.raises(CommandError, match=error):
        backup.restore(S, server, name)
    assert not incus.ran("incus stop") and not backup.RESTORE.exists()


def test_a_restore_cut_short_goes_on_where_it_stopped(server, nas, incus):
    backup.create(S, server)
    nas.down = False
    original = nas.answer

    def cut_off(cmd, env, cwd=None):   # the backup machine goes away during the import
        if cmd[0] == "bash":
            nas.down = True
        return original(cmd, env, cwd)
    nas.answer = cut_off
    with pytest.raises(CommandError):
        backup.restore(S, server, "alice")
    assert "alice" not in incus.containers and json.loads(backup.RESTORE.read_text())["step"] == "replace"
    # meanwhile, no backup, and no other restore
    assert backup.create(S, server) == 1
    with pytest.raises(CommandError, match="the restore of alice is unfinished"):
        backup.restore(S, server, "bob")

    nas.answer, nas.down = original, False
    backup.restore(S, server, "alice")
    assert chats(incus, "alice") == "alice's chats" and server.get("alice").state == "active"
    assert len([name for name in nas.archives if name.startswith("before-restore.")]) == 1   # it was gone by then


def new_server(incus, tmp_path):
    """This server, wiped as a new one is after install.sh, init and backup init: its own config.toml, key to the
    backups and passphrase, no instances."""
    state = tmp_path / "state"
    shutil.rmtree(state)
    state.mkdir()
    settings.CONFIG_TOML.write_text(CONFIG)
    settings.SECRETS_ENV.write_text("THINNY_BACKUP_PASSPHRASE=kept-in-a-password-manager\n")
    shutil.rmtree(settings.CONFIG_TOML.parent / "groups", ignore_errors=True)
    incus.containers.clear()
    incus.pools.clear()
    return Registry()


@pytest.fixture
def old_server(server, nas, incus, tmp_path):
    """The old server as its last backup has it: its own settings, a token, a group, alice's access trace and the
    office's certificate authority; alice and bob at 10.78.0.1 and 10.78.0.2."""
    settings.CONFIG_TOML.write_text(CONFIG + "[container]\ncpu = 3\n")
    settings.SECRETS_ENV.write_text("THINNY_BACKUP_PASSPHRASE=kept-in-a-password-manager\n"
                                    "THINNY_PURELYMAIL_API_TOKEN=pm-live-x\n")
    (settings.CONFIG_TOML.parent / "groups").mkdir()
    (settings.CONFIG_TOML.parent / "groups" / "sales.toml").write_text('[agent]\nenvironment_hint = "Sales"\n')
    (trace.DIR / "alice").mkdir(parents=True)
    (trace.DIR / "alice" / "admin-access.log").write_text("2026-11-10 incus exec alice\n")
    ca = host.caddy_root_ca().parent
    ca.mkdir(parents=True)
    (ca / "root.key").write_text("the office's certificate authority")
    for i, name in enumerate(("alice", "bob")):
        server.get(name).address = f"10.78.0.{i + 1}"
    server.save()
    assert backup.create(S, server) == 0
    (ca / "root.key").write_text("the one Caddy made on the new server")
    (ca.parents[2] / "certificates" / "local").mkdir(parents=True)
    return runs(nas)[-1]


def test_a_new_server_gets_the_old_ones_settings_and_instances_as_they_were(old_server, nas, incus, tmp_path, capsys):
    reg = new_server(incus, tmp_path)
    capsys.readouterr()
    backup.restore_all(S, reg)

    # its settings, tokens, group and access traces; its own key to the backups and passphrase stay
    restored = settings.load(settings.CONFIG_TOML)
    assert restored.container_cpu == 3 and restored.groups["sales"]
    assert settings.secret("THINNY_PURELYMAIL_API_TOKEN") == "pm-live-x"
    assert (trace.DIR / "alice" / "admin-access.log").read_text() == "2026-11-10 incus exec alice\n"
    assert backup.KEY.read_text() == "PRIVATE\n"
    # applied before any container came, so they start under its firewall and limits
    [(applied, containers)] = nas.applied
    assert applied.container_cpu == 3 and containers == []
    # each instance as it was, at its own port and address; disabled carol stays stopped; removed dave stays removed
    reg = Registry.load()
    assert {name: (inst.state, inst.port, inst.address) for name, inst in reg.instances.items()} == {
        "alice": ("active", 9120, "10.78.0.1"), "bob": ("active", 9121, "10.78.0.2"),
        "carol": ("disabled", 9122, ""), "dave": ("removed", 9123, "")}
    assert {name: c["state"] for name, c in incus.containers.items()} == \
        {"alice": "RUNNING", "bob": "RUNNING", "carol": "STOPPED"}
    assert chats(incus, "alice") == "alice's chats" and chats(incus, "carol") == "carol's chats"
    assert incus.pools["thinny-alice"]["size"] == 30 * 2**30
    assert incus.containers["alice"]["devices"]["eth0"]["ipv4.address"] == "10.78.0.1"
    assert not incus.ran("display.language")   # its users' settings came along
    # the office PCs keep trusting its sites
    ca = host.caddy_root_ca().parent
    assert (ca / "root.key").read_text() == "the office's certificate authority"
    assert not (ca.parents[2] / "certificates" / "local").exists()
    assert nas.ran("chown -R caddy:caddy") and nas.ran("systemctl start caddy")
    assert not backup.RESTORE.exists()
    assert f"This server is back as it was in {old_server}." in capsys.readouterr().out


def test_after_an_unfinished_update_every_instance_goes_to_its_release(server, nas, incus, tmp_path):
    update.UNFINISHED.write_text("v3 c3 bob\n")   # alice may be at v3 already, so none may stay at v2
    backup.create(S, server)
    backup.restore_all(S, new_server(incus, tmp_path))
    assert host.RELEASE_FILE.read_text() == "v3 c3\n" and not update.UNFINISHED.exists()


def test_a_restore_of_everything_cut_short_goes_on_where_it_stopped(old_server, nas, incus, tmp_path):
    reg = new_server(incus, tmp_path)
    nas.archives[f"{old_server}.instance.bob"]["data"] = b"cut off"
    with pytest.raises(CommandError, match="incus import"):
        backup.restore_all(S, reg)
    assert json.loads(backup.RESTORE.read_text())["step"] == "instances"
    assert backup.create(S, Registry.load()) == 1   # no backups meanwhile
    with pytest.raises(CommandError, match="the restore of everything is unfinished"):
        backup.restore(S, Registry.load(), "alice")

    nas.archives[f"{old_server}.instance.bob"]["data"] = json.dumps(incus_container("bob")).encode()
    incus.calls.clear()
    backup.restore_all(S, Registry.load())
    assert not incus.ran("incus import - alice") and incus.ran("incus import - bob")
    assert Registry.load().get("bob").state == "active" and not backup.RESTORE.exists()


def incus_container(name):
    return {"state": "RUNNING", "devices": {}, "account": True, "hermes": True, "unit": True,
            "files": {"/home/x/.hermes/state.db": f"{name}'s chats"}}


def test_a_restore_of_everything_needs_a_new_server_and_a_complete_backup(old_server, nas, incus, tmp_path):
    with pytest.raises(CommandError, match="this server has instances"):
        backup.restore_all(S, Registry.load())
    reg = new_server(incus, tmp_path)
    with pytest.raises(CommandError, match="no complete backup 2026-11-01T03:00:00"):
        backup.restore_all(S, reg, "2026-11-01T03:00:00")
    assert not backup.RESTORE.exists()


@pytest.mark.parametrize("why", ["incomplete", "taken"])
def test_a_restore_of_everything_refuses_before_changing_anything(old_server, nas, incus, tmp_path, why):
    reg = new_server(incus, tmp_path)
    if why == "incomplete":   # e.g. a cleanup cut short
        del nas.archives[f"{old_server}.instance.carol"]
        error = f"{old_server} lacks carol's backup"
    else:   # a container of the new server's own
        incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "files": {}}
        error = "an Incus container 'alice' already exists"
    with pytest.raises(CommandError, match=error):
        backup.restore_all(S, reg)
    assert settings.CONFIG_TOML.read_text() == CONFIG and not backup.RESTORE.exists()
    assert not nas.applied and incus.containers.get("alice", {}).get("files") in (None, {})


def test_a_restored_config_naming_an_interface_this_server_lacks_stops_before_the_instances(old_server, nas, incus,
                                                                                            tmp_path):
    reg = new_server(incus, tmp_path)
    shutil.rmtree(tmp_path / "net" / "eth0")
    with pytest.raises(CommandError, match="no network interface eth0"):
        backup.restore_all(S, reg)
    assert not incus.containers and not nas.applied
    (tmp_path / "net" / "eth0").mkdir()   # once thinny config edit corrected it
    backup.restore_all(S, Registry.load())
    assert Registry.load().get("alice").state == "active"
