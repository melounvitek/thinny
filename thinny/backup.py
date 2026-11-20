"""Backups with borg over SSH, to the machine and folder in [backup] target: every instance's container and the
server's own state, nightly before the update, and restores from them (README, Backups).

thinny's own SSH key there runs only `borg serve --append-only`, so a server broken into cannot delete the backups
for good: they come back until the backup machine's monthly cleanup (its crontab) compacts the repository. The
repository's key is encrypted with one passphrase, which the admin keeps; init leaves a copy of that key beside the
repository, out of the restricted key's reach, in case a server broken into replaces the one in it."""

from __future__ import annotations

import getpass
import posixpath
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from . import settings
from .settings import Settings, secret
from .system import CommandError, log, run, write_file

KEY = settings.CONFIG_TOML.parent / "backup-key"   # thinny's SSH key on the backup machine, with KEY.pub beside it
KNOWN_HOSTS = settings.CONFIG_TOML.parent / "backup-known-hosts"   # the backup machine's host key, confirmed at init
PASSPHRASE = "THINNY_BACKUP_PASSPHRASE"
LOCK_WAIT = "600"   # seconds a borg command waits for another's lock, such as the monthly cleanup's
NO_BORG = 3   # the exit code of a remote step that lacks a command there


def target(s: Settings) -> tuple[str, str]:
    """(user@host, path) of the backups."""
    if not s.backup_target:
        raise CommandError("backups are off: set [backup] target in config.toml first (thinny config edit)")
    account, _, path = s.backup_target.partition(":")
    return account, path


def ssh_options() -> list[str]:
    """thinny's key only, never asking, never through another connection: the backup machine runs borg serve for
    it, which may only add backups."""
    return ["-i", str(KEY), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]


def borg(s: Settings, *args: str, passphrase: str | None = None, **kw) -> subprocess.CompletedProcess:
    """borg on the backups, through thinny's key."""
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8",
           "BORG_REPO": s.backup_target, "BORG_RSH": shlex.join(["ssh", *ssh_options()]),
           "BORG_PASSPHRASE": secret(PASSPHRASE) if passphrase is None else passphrase}
    return run(["borg", *args], env=env, **kw)


def init(s: Settings) -> None:
    """Set up the backups, once per server: thinny's key on the backup machine, restricted to adding backups, the
    repository and its passphrase (a new one, or the one the admin kept), the copy of its key and the monthly
    cleanup there. The admin logs in to the backup machine once, as the account in [backup] target; thinny keeps
    nothing of that login. Repeatable."""
    account, path = target(s)
    if not sys.stdin.isatty():
        raise CommandError("no terminal to ask on")
    if not shutil.which("borg"):
        log("installing borg")
        run(["apt-get", "install", "-y", "-q", "borgbackup"], env={"DEBIAN_FRONTEND": "noninteractive",
                                                                  "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, timeout=900)
    if not KEY.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"thinny-backup@{socket.gethostname()}",
             "-f", str(KEY)])
    public = Path(f"{KEY}.pub").read_text().strip()
    with tempfile.TemporaryDirectory() as private:   # root's only
        master = Path(private) / "ssh"
        print(f"Logging in to {account} to set it up; thinny keeps nothing of this login.")
        login(account, master)
        try:
            remote = Remote(account, master)
            found = remote.run('borg=$(command -v borg) || exit 3; cd && printf "%s\\n%s\\n" "$borg" "$PWD"',
                               check=False)
            if found.returncode == NO_BORG:
                raise CommandError(f"borg is not installed on {account.partition('@')[2]}: install it there "
                                   "(e.g. apt install borgbackup), then run thinny backup init again")
            if found.returncode:
                raise CommandError(f"{account}: {found.stdout}{found.stderr}".strip())
            remote_borg, home = found.stdout.splitlines()[:2]
            repo = posixpath.normpath(posixpath.join(home, path))
            authorize(remote, public, f'command="{remote_borg} serve --append-only --restrict-to-repository {repo}",'
                                      f"restrict {public}")
            check_restricted(account)
            passphrase = secret(PASSPHRASE)
            new = remote.run('test -f "$1/config"', repo, check=False).returncode != 0
            if new:
                passphrase = passphrase or secrets.token_urlsafe(24)
                save_passphrase(passphrase)   # first: an init that fails later reuses it
                borg(s, "init", "--encryption", "repokey-blake2", passphrase=passphrase, timeout=300)
            else:
                passphrase = ask_passphrase(s, passphrase)
            # Only when missing: the repository's key may be a broken-in server's by now.
            remote.run('[ -e "$2" ] || "$3" key export "$1" "$2"', repo, f"{repo}.key-copy", remote_borg)
            cleanup = (f"0 12 1 * * {remote_borg} check --repository-only --lock-wait {LOCK_WAIT} {repo} && "
                       f"{remote_borg} compact --lock-wait {LOCK_WAIT} {repo}")
            scheduled = remote.run('command -v crontab >/dev/null || exit 3; crontab -l 2>/dev/null | grep -qF -- "$1" '
                                   '|| { crontab -l 2>/dev/null; printf "%s\\n" "$2"; } | crontab -',
                                   f"compact --lock-wait {LOCK_WAIT} {repo}", cleanup, check=False)
            if scheduled.returncode == NO_BORG:
                print(f"{account.partition('@')[2]} has no crontab: schedule this monthly in its task scheduler, as "
                      f"{account.partition('@')[0]}:\n  {cleanup.split(' ', 5)[5]}")
            elif scheduled.returncode:
                raise CommandError(f"{account}: crontab: {scheduled.stdout}{scheduled.stderr}".strip())
        finally:
            run(["ssh", "-o", f"ControlPath={master}", "-O", "exit", account], check=False, timeout=30)
    log(f"backups set up on {s.backup_target}")
    if new:
        print(f"\nThe backups' passphrase, shown only now:\n\n  {passphrase}\n\n"
              "Keep it off this server, e.g. in your password manager: without it, nothing can be restored.")
        input("Press Enter once it is saved. ")
    print("Backups run every night before the update; thinny backup takes one now.")


def login(account: str, master: Path) -> None:
    """Log in as the admin, who confirms the backup machine's host key and types a password or uses their SSH key,
    for a connection the next steps share. It closes 60 s after the last one, or when init closes it."""
    code = subprocess.run(["ssh", "-o", "ControlMaster=yes", "-o", f"ControlPath={master}", "-o", "ControlPersist=60",
                           "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=ask",
                           account, "true"]).returncode
    if code:
        raise CommandError(f"could not log in to {account} (sudo hides your SSH agent: use a password, or run "
                           "sudo --preserve-env=SSH_AUTH_SOCK thinny backup init)")


class Remote:
    """Commands on the backup machine through the admin's login."""

    def __init__(self, account: str, master: Path):
        self.account, self.master = account, master

    def run(self, script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return run(["ssh", "-o", f"ControlPath={self.master}", "-o", "ControlMaster=no", "-o", "BatchMode=yes",
                    self.account, shlex.join(["sh", "-c", script, "sh", *args])], check=check, timeout=300)


def authorize(remote: Remote, public: str, line: str) -> None:
    """thinny's key in the account's authorized_keys there with `line`, its options, besides the other keys."""
    current = remote.run("cat ~/.ssh/authorized_keys 2>/dev/null || true").stdout.splitlines()
    mine = [entry for entry in current if public.split()[1] in entry.split()]
    if mine == [line]:
        return
    if mine:
        raise CommandError(f"thinny's key is in ~/.ssh/authorized_keys on {remote.account} already, with other "
                           f"options: delete its line there (ending in {public.split()[2]}), then run init again")
    remote.run('umask 077 && mkdir -p ~/.ssh && f=~/.ssh/authorized_keys && '
               '{ [ ! -s "$f" ] || [ -z "$(tail -c 1 "$f")" ] || echo >> "$f"; } && printf "%s\\n" "$1" >> "$f"', line)


def check_restricted(account: str) -> None:
    """Refuse a key that runs more than borg serve there: another entry for it could allow a shell."""
    res = run(["ssh", *ssh_options(), account, "echo", "thinny-unrestricted"], check=False, timeout=120)
    if res.returncode == 255:
        raise CommandError(f"thinny's key cannot log in to {account}: {res.stderr.strip()}")
    if "thinny-unrestricted" in res.stdout:
        raise CommandError(f"thinny's key runs any command on {account}, not only borg serve: check "
                           "~/.ssh/authorized_keys and sshd_config there")


def ask_passphrase(s: Settings, passphrase: str) -> str:
    """The passphrase of the repository there, the one in secrets.env or else asked for until it opens it."""
    while True:
        if not passphrase:
            passphrase = getpass.getpass("The backups' passphrase: ").strip()
            if not passphrase:
                continue
        res = borg(s, "info", passphrase=passphrase, check=False, timeout=300)
        if res.returncode == 0:
            save_passphrase(passphrase)
            return passphrase
        if "passphrase" not in res.stdout + res.stderr:
            raise CommandError(f"borg info: {(res.stdout + res.stderr).strip()}")
        print("That passphrase does not open the backups.")
        passphrase = ""


def save_passphrase(passphrase: str) -> None:
    path = settings.SECRETS_ENV
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition("=")[0].strip() != PASSPHRASE] + [f"{PASSPHRASE}={passphrase}"]
    write_file(path, "\n".join(lines) + "\n", 0o600)
