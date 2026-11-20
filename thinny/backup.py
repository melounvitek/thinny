"""Backups with borg over SSH, to the machine and folder in [backup] target: every instance's container and the
server's own state, nightly before the update, and restores from them (README, Backups).

thinny's own SSH key there runs only `borg serve --append-only`, so a server broken into cannot delete the backups
for good: they come back until the backup machine's monthly cleanup (its crontab) compacts the repository. The
repository's key is encrypted with one passphrase, which the admin keeps; init leaves a copy of that key beside the
repository, out of the restricted key's reach, in case a server broken into replaces the one in it.

A nightly run makes one archive per container, `<run>.instance.<name>`, and then `<run>.server` with the server's
settings and state: a run without it is incomplete, and the next one deletes what it left. `<run>` is its local
time, ISO 8601."""

from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import json
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from . import apply, export, host, instances, settings, trace, update
from .registry import Instance, Registry
from .settings import Settings, secret
from .system import STATE_DIR, CommandError, log, run, step, write_file

KEY = settings.CONFIG_TOML.parent / "backup-key"   # thinny's SSH key on the backup machine, with KEY.pub beside it
KNOWN_HOSTS = settings.CONFIG_TOML.parent / "backup-known-hosts"   # the backup machine's host key, confirmed at init
PASSPHRASE = "THINNY_BACKUP_PASSPHRASE"
LOCK_WAIT = "600"   # seconds a borg command waits for another's lock, such as the monthly cleanup's
NO_BORG = 3   # the exit code of a remote step that lacks a command there
STATUS_FILE = STATE_DIR / "backup-status.json"   # the last run's; any account may read it (extras/nightly-watch)
RESTORE = STATE_DIR / "restore.json"   # an unfinished restore: what it restores, from which archive, how far it got
KEEP = ["--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6"]
ARCHIVE_TIMEOUT = 6 * 3600   # a large container's
RUN_GLOB = "????-??-??T??:??:??"   # a run's name
NETWORK_INTERFACES = Path("/sys/class/net")


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


def borg_env(s: Settings, passphrase: str | None = None) -> dict[str, str]:
    """borg's environment for the backups, through thinny's key."""
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8",
            "BORG_REPO": s.backup_target, "BORG_RSH": shlex.join(["ssh", *ssh_options()]),
            "BORG_PASSPHRASE": secret(PASSPHRASE) if passphrase is None else passphrase}


def borg(s: Settings, *args: str, passphrase: str | None = None, **kw) -> subprocess.CompletedProcess:
    return run(["borg", args[0], "--lock-wait", LOCK_WAIT, *args[1:]], env=borg_env(s, passphrase), **kw)


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
        if "passphrase" not in (res.stdout + res.stderr).lower():
            raise CommandError(f"borg info: {(res.stdout + res.stderr).strip()}")
        print("That passphrase does not open the backups.")
        passphrase = ""


def save_passphrase(passphrase: str) -> None:
    path = settings.SECRETS_ENV
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition("=")[0].strip() != PASSPHRASE] + [f"{PASSPHRASE}={passphrase}"]
    write_file(path, "\n".join(lines) + "\n", 0o600)


def create(s: Settings, reg: Registry, scheduled: bool = False) -> int:
    """Back up every container (without stopping it: Incus exports a snapshot) and then the server, then delete
    what earlier failed runs left and old backups. 1 when it failed: thinny-update.service runs it before the nightly
    update, which then waits for a run that worked."""
    if not s.backup_target and scheduled:
        return 0
    target(s)
    started = dt.datetime.now().replace(microsecond=0)
    run_name = started.isoformat()
    timestamp = started.astimezone(dt.UTC).replace(tzinfo=None).isoformat()   # borg takes UTC: buckets per run
    log(f"=== thinny backup {run_name}")
    try:
        if RESTORE.exists():   # a container may be missing, or the one from before the restore
            name = json.loads(RESTORE.read_text())["name"]
            raise CommandError(f"a restore is unfinished: run thinny backup restore {name or '--all'} again")
        states, sizes = instances.container_states(), instances.pool_sizes()
        # Also disabled ones, which keep their data, and pending ones with a container.
        containers = sorted((inst for inst in reg.instances.values() if inst.name in states), key=lambda i: i.port)
        try:
            for inst in containers:
                archive(s, inst, f"{run_name}.instance.{inst.name}", timestamp, sizes[instances.pool_of(inst.name)])
            ca = host.caddy_root_ca().parent   # the office PCs trust it
            # Not the new server's own key there, nor exports, which are their users'.
            excluded = [KEY, f"{KEY}.pub", KNOWN_HOSTS, export.EXPORTS]
            borg(s, "create", "--timestamp", timestamp, *[f"--exclude=pp:{path}" for path in excluded],
                 f"::{run_name}.server", str(settings.CONFIG_TOML.parent), str(STATE_DIR),
                 *([str(ca)] if ca.exists() else []), timeout=3600)
        except BaseException:
            borg(s, "delete", "--glob-archives", f"{run_name}.*", check=False, timeout=3600)
            raise
        tidy(s)
        result, detail = "ok", f"{len(containers)} instances and the server"
    except Exception as e:  # noqa: BLE001 - any failure must end as FAILED
        result, detail = "FAILED", str(e) or type(e).__name__
    log(f"{result}: {detail}")
    last = started if result == "ok" else last_success()
    status = {"time": started.isoformat(), "result": result, "detail": detail,
              "last_success": last.isoformat() if last else None}
    write_file(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=1) + "\n")
    return 1 if result == "FAILED" else 0


def archive(s: Settings, inst: Instance, name: str, timestamp: str, disk: int) -> None:
    """The instance's container as Incus exports it, uncompressed so that borg stores only what changed. Its comment
    has what a restore needs: the Hermes release its server was at (or going to), its built-ins and disk size."""
    folder = Path(instances.SERVER_DISK) / "storage-pools" / instances.pool_of(inst.name) / "containers" / inst.name
    check_room(inst.name, int(run(["du", "-sb", str(folder)], timeout=3600).stdout.split()[0]), "back up")
    tag, commit = (update.UNFINISHED.read_text().split()[:2] if update.UNFINISHED.exists() else
                   host.release() or ("", ""))
    comment = json.dumps({"release": [tag, commit], "built_ins": inst.built_ins, "disk_gb": disk // 2**30})
    comment = comment.replace("{", "{{").replace("}", "}}")   # borg fills in its {placeholders} there
    with step(f"{inst.name}: backup"):
        borg(s, "create", "--timestamp", timestamp, "--comment", comment, "--content-from-command",
             "--stdin-name", f"{inst.name}.tar", f"::{name}", "--",
             "incus", "export", inst.name, "-", "--instance-only", "--compression", "none", "--quiet",
             timeout=ARCHIVE_TIMEOUT)


def check_room(name: str, size: int, doing: str) -> None:
    """Refuse an archive of `size` bytes Incus has no room for: it writes the whole of it to the server's disk before
    borg reads it or before unpacking it, and never into the last tenth of that disk, where the instances' pools
    grow."""
    disk = shutil.disk_usage(instances.SERVER_DISK)
    room = max(0, disk.free - disk.total * export.KEEP_FREE)
    if room < size:
        raise CommandError(f"the server needs {size / 2**30:.1f} GiB free on top of 10% of its disk to {doing} {name}, "
                           f"for the archive Incus writes first; it has {room / 2**30:.1f} GiB")


def archives(s: Settings) -> list[str]:
    return [a["name"] for a in json.loads(borg(s, "list", "--json", timeout=600).stdout)["archives"]]


def tidy(s: Settings) -> None:
    """Delete what failed runs left, then old backups: KEEP of each instance's and the server's, and the state
    before a restore for 30 days. Their space comes free at the backup machine's monthly cleanup (at once where
    nothing restricts thinny's key)."""
    complete, members = runs(archives(s))
    for unfinished in sorted(set(members) - set(complete)):
        borg(s, "delete", "--glob-archives", f"{unfinished}.instance.*", timeout=3600)
    for name in sorted({name for names in members.values() for name in names}):
        borg(s, "prune", *KEEP, "--glob-archives", f"*.instance.{name}", timeout=3600)
    borg(s, "prune", *KEEP, "--glob-archives", f"{RUN_GLOB}.server", timeout=3600)
    borg(s, "prune", "--keep-within", "30d", "--glob-archives", "before-restore.*", timeout=3600)
    borg(s, "compact", timeout=6 * 3600)   # with --append-only it frees nothing, and says nothing


def runs(names: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """The complete runs among these archives, and each run's instances. A run's name has no dot; an instance named
    server is `<run>.instance.server`."""
    complete, members = [], {}
    for name in names:
        run_name, _, rest = name.partition(".")
        if rest == "server":
            complete.append(run_name)
        elif rest.startswith("instance."):
            members.setdefault(run_name, []).append(rest.removeprefix("instance."))
    return sorted(complete), members


def last_success() -> dt.datetime | None:
    try:
        last = json.loads(STATUS_FILE.read_text(encoding="utf-8")).get("last_success")
    except FileNotFoundError:
        return None
    return dt.datetime.fromisoformat(last) if last else None


def list_backups(s: Settings) -> None:
    """Each complete run with its instances, and the instances' states before a restore: what restore takes."""
    target(s)
    names = archives(s)
    complete, members = runs(names)
    for run_name in complete:
        print(f"{run_name}  {', '.join(sorted(members.get(run_name, []))) or 'no instances'}")
    for name in sorted(name for name in names if name.startswith("before-restore.")):
        print(f"{name}  {name.split('.')[1]} as it was before a restore")


def restore(s: Settings, reg: Registry, name: str, source: str | None = None) -> None:
    """Put the instance back as it was in a backup (by default, the newest): stop it, back up its state now (the way
    back), replace its container with the backup's, then finish it like a moved instance, at this server's Hermes
    release. RESTORE records each step, so a run again goes on where one stopped."""
    target(s)
    marker = unfinished(name)
    inst = reg.get(name)
    if marker:
        log(f"{name}: going on with its restore from {marker['archive']}")
    else:
        if inst.state != "active":
            raise CommandError(f"{name} is {inst.state}; only an active instance is restored")
        if update.UNFINISHED.exists():
            raise CommandError("an update is unfinished: run thinny update --all first")
        source_name = pick(archives(s), name, source)
        info = json.loads(borg(s, "info", "--json", f"::{source_name}", timeout=600).stdout)["archives"][0]
        details = json.loads(info["comment"])
        check_release(details["release"])
        check_room(name, info["stats"]["original_size"], "restore")
        marker = {"name": name, "archive": source_name, "built_ins": details["built_ins"],
                  "disk_gb": details["disk_gb"], "step": "stop"}
        write_file(RESTORE, json.dumps(marker) + "\n", 0o600)
    if marker["step"] == "stop":
        instances.stop(name)
        now = dt.datetime.now().replace(microsecond=0)
        fallback = f"before-restore.{name}.{now.isoformat()}"
        archive(s, inst, fallback, now.astimezone(dt.UTC).replace(tzinfo=None).isoformat(),
                instances.pool_sizes()[instances.pool_of(name)])
        marker |= {"fallback": fallback, "step": "replace"}
        write_file(RESTORE, json.dumps(marker) + "\n", 0o600)
    if marker["step"] == "replace":
        if instances.container_state(name) is not None:
            run(["incus", "delete", "--force", name])   # its pool stays, for the backup's
        if instances.pool_sizes()[instances.pool_of(name)] < marker["disk_gb"] * 2**30:   # a purged name's new disk
            instances.set_limits(name, None, None, marker["disk_gb"])
        import_container(s, marker["archive"], name)
        inst.state, inst.moved, inst.built_ins = "pending", True, marker["built_ins"]
        reg.save()
        marker["step"] = "finish"
        write_file(RESTORE, json.dumps(marker) + "\n", 0o600)
    if inst.state != "active":   # else instance add finished it
        instances.add(s, reg, name, inst.access, inst.url if inst.access == "url" else "")
    RESTORE.unlink()
    print(f"{name} is back as it was in {marker['archive']}. As it was just before, it is in the backup "
          f"{marker['fallback']}: thinny backup restore {name} --from {marker['fallback']}")


def unfinished(name: str | None) -> dict | None:
    """The unfinished restore of `name` (None: of everything) to go on with, or none; refuses another one's."""
    marker = json.loads(RESTORE.read_text()) if RESTORE.exists() else None
    if marker and marker["name"] != name:
        raise CommandError(f"the restore of {marker['name'] or 'everything'} is unfinished: run thinny backup restore "
                           f"{marker['name'] or '--all'} again")
    return marker


def pick(names: list[str], name: str, source: str | None) -> str:
    """The archive of the instance: of the run `source`, the state before a restore `source`, or the newest."""
    if source is None:
        complete, members = runs(names)
        found = [run_name for run_name in complete if name in members.get(run_name, [])]
        if not found:
            raise CommandError(f"no backup of {name} (see thinny backup list)")
        return f"{found[-1]}.instance.{name}"
    for candidate in (f"{source}.instance.{name}", source if source.startswith(f"before-restore.{name}.") else ""):
        if candidate in names:
            return candidate
    raise CommandError(f"no backup {source} of {name} (see thinny backup list)")


def check_release(release: list[str]) -> None:
    """Refuse a backup from a newer Hermes release than this server's (or one it cannot tell): Hermes databases
    cannot be downgraded. An older one's moves on to this server's."""
    def version(tag: str) -> tuple[int, ...] | None:
        return tuple(map(int, m[1].split("."))) if (m := re.fullmatch(r"v?(\d+(?:\.\d+)*)", tag)) else None
    here = (host.release() or ("",))[0]
    if release[0] != here and not (version(release[0]) and version(here) and version(release[0]) < version(here)):
        raise CommandError(f"the backup is of Hermes {release[0] or 'unknown'}, this server runs {here or 'none'}: "
                           "Hermes databases cannot be downgraded")


def import_container(s: Settings, archive_name: str, name: str) -> None:
    """The archive's container, as the instance's, into its pool."""
    with step(f"{name}: importing its container"):
        run(["bash", "-o", "pipefail", "-c", 'borg extract --lock-wait "$1" --stdout "::$2" | '
             'incus import - "$3" --storage "$4" --quiet', "bash", LOCK_WAIT, archive_name, name,
             instances.pool_of(name)], env=borg_env(s), timeout=ARCHIVE_TIMEOUT)


def restore_all(s: Settings, reg: Registry, source: str | None = None) -> None:
    """On a new server, once init connected it to the backups: this server's settings, tokens and instances as they
    were in a run (by default the newest), each instance at the port and address it had, at the Hermes release its
    server ran or was going to; then its certificate authority. RESTORE records how far it got; run again, it goes
    on."""
    target(s)
    marker = unfinished(None)
    if not marker:
        if reg.instances:
            raise CommandError("this server has instances; restore --all is for a new one (restore them one by one)")
        complete, _ = runs(archives(s))
        run_name = source or (complete[-1] if complete else "")
        if run_name not in complete:
            raise CommandError(f"no complete backup {source} (see thinny backup list)" if source else "no backup yet")
        marker = {"name": None, "run": run_name, "step": "files"}
        write_file(RESTORE, json.dumps(marker) + "\n", 0o600)
    run_name = marker["run"]
    if marker["step"] == "files":
        with server_files(s, run_name) as root:
            restore_files(root)
        marker["step"] = "instances"
        write_file(RESTORE, json.dumps(marker) + "\n", 0o600)
    s, reg = settings.load(settings.CONFIG_TOML), Registry.load()   # the old server's
    if not (NETWORK_INTERFACES / s.network_interface).exists():
        raise CommandError(f"this server has no network interface {s.network_interface} (network.interface in the "
                           "restored config.toml): correct it with thinny config edit, then run thinny backup "
                           "restore --all again")
    apply.apply(s)   # firewall, profile and units as the restored config.toml says, before any container starts
    names = archives(s)
    for inst in sorted(reg.instances.values(), key=lambda inst: inst.port):
        source_name = f"{run_name}.instance.{inst.name}"
        if source_name in names and instances.container_state(inst.name) is None:
            info = json.loads(borg(s, "info", "--json", f"::{source_name}", timeout=600).stdout)["archives"][0]
            host.write_policy(s, inst.name)   # Incus refuses the container while its policy's folder is missing
            if instances.pool_of(inst.name) not in instances.pool_sizes():
                instances.create_pool(inst.name, json.loads(info["comment"])["disk_gb"])
            check_room(inst.name, info["stats"]["original_size"], "restore")
            import_container(s, source_name, inst.name)
        if inst.moved and inst.state == "pending" and instances.container_state(inst.name) is not None:
            instances.add(s, reg, inst.name, inst.access, inst.url if inst.access == "url" else "")
    with server_files(s, run_name) as root:
        restore_ca(s, reg, root)
    RESTORE.unlink()
    print(f"This server is back as it was in {run_name}. Check access.lan_address in config.toml if its LAN "
          "address changed (thinny config edit), and sign it in to Tailscale if its instances use it. On the backup "
          "machine, delete the old server's line in ~/.ssh/authorized_keys.")


@contextlib.contextmanager
def server_files(s: Settings, run_name: str):
    """The folder the run's server archive is unpacked in, as it was under /."""
    with tempfile.TemporaryDirectory(dir=STATE_DIR) as root:   # root's only, with tokens
        borg(s, "extract", f"::{run_name}.server", cwd=root, timeout=3600)
        yield Path(root)


def restore_files(root: Path) -> None:
    """Settings, tokens, groups, instances, releases and access traces from the old server; this server keeps its
    own key to the backups. Instances that were active become pending, moved: imported, they finish like moved ones.
    An unfinished update's release is the release, so that every instance only moves forward."""
    def old(path: Path) -> Path:
        return root / str(path).lstrip("/")
    for path in (settings.CONFIG_TOML, settings.SECRETS_ENV):
        if old(path).exists():
            shutil.copy2(old(path), path)
    for folder in (settings.CONFIG_TOML.parent / "groups", trace.DIR):
        if old(folder).exists():
            shutil.copytree(old(folder), folder, dirs_exist_ok=True)
    for path, into in ((host.RELEASE_FILE, host.RELEASE_FILE), (host.WEBUI_RELEASE_FILE, host.WEBUI_RELEASE_FILE),
                       (update.UNFINISHED, host.RELEASE_FILE)):   # its "<tag> <commit> [<instance>]"
        if old(path).exists():
            host.write_release(*old(path).read_text().split()[:2], into)
    reg = Registry.load(old(STATE_DIR / "instances.json"))
    for inst in reg.instances.values():
        if inst.state == "active":
            inst.state, inst.moved = "pending", True
    reg.save()


def restore_ca(s: Settings, reg: Registry, root: Path) -> None:
    """The old server's certificate authority, which office PCs trust, in place of the one Caddy made here; Caddy then
    issues the sites' certificates afresh."""
    ca = host.caddy_root_ca().parent
    old = root / str(ca).lstrip("/")
    if not (old.exists() and shutil.which("caddy")):
        return
    run(["systemctl", "stop", "caddy"])
    shutil.rmtree(ca, ignore_errors=True)
    shutil.rmtree(ca.parents[2] / "certificates" / "local", ignore_errors=True)
    shutil.copytree(old, ca)
    run(["chown", "-R", "caddy:caddy", str(ca)])
    run(["systemctl", "start", "caddy"])
    host.reconcile_access(s, reg)   # the office's copy of it
