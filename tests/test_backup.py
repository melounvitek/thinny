import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from thinny import backup, settings
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
            return self.borg(cmd[1:], env)
        return "", "", 0

    def repo(self, env):
        return self.home / env["BORG_REPO"].partition(":")[2]

    def borg(self, args, env):
        repo = self.repo(env)
        if args[0] == "init":
            repo.mkdir()
            (repo / "config").write_text("[repository]\n")
            self.passphrases[repo] = env["BORG_PASSPHRASE"]
        elif args[0] == "info" and self.passphrases.get(repo) != env["BORG_PASSPHRASE"]:
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
    assert nas.ran("borg init --encryption repokey-blake2")
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
