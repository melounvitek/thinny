"""Moving whole instance containers to another thinny server: `incus export` here, over SSH, `incus import`
there, where the instance gets that server's port, address and settings and keeps everything inside."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO

from . import host, instances, update
from .registry import Instance, Registry, RegistryError
from .settings import Settings
from .system import STATE_DIR, CommandError, log, run

ARCHIVE_TIMEOUT = 6 * 3600   # an export or import of a large container


def move(s: Settings, reg: Registry, names: list[str], to: str) -> None:
    """Move the named instances, or every active one, to the thinny server `to` ([user@]host), one at a
    time; each stays here, disabled. That server checks them all before any stops here."""
    targets = [reg.get(n) for n in names] if names else [inst for inst in reg.current() if inst.state == "active"]
    for inst in targets:
        if inst.state != "active":
            raise RegistryError(f"{inst.name} is {inst.state}")
    if update.UNFINISHED.exists():
        raise CommandError("an update is unfinished on this server; run thinny update --all on both servers first")
    release = host.release()
    sizes = instances.pool_sizes()
    disks = {inst.name: sizes[instances.pool_of(inst.name)] // 2**30 for inst in targets}
    needed = 0
    for inst in targets:
        needed += disks[inst.name]   # with room for the disks before it: that server would get them all
        if remote(to, receive_args(inst, release, needed, check=True)):
            raise CommandError(f"{to} cannot take {inst.name} (see above); nothing moved")
    for inst in targets:
        if inst.mailbox:
            print(f"{inst.name} has the mailbox {inst.mailbox}. It goes with {inst.name}"
                  f"{'' if inst.own_mailbox else f', and {to} deletes it with {inst.name}'}. After the move, check "
                  f"there that {inst.name} still gets and sends mail.")

    for inst in targets:
        name = inst.name
        log(f"{name}: moving to {to} (its container stops for a few minutes)")
        instances.stop(name)
        try:
            with tempfile.TemporaryDirectory(dir=STATE_DIR) as private:   # the archive holds the instance's secrets
                path = Path(private) / f"{name}.tar.gz"
                run(["incus", "export", name, str(path), "--instance-only", "--quiet"], timeout=ARCHIVE_TIMEOUT)
                with path.open("rb") as f:
                    code = remote(to, receive_args(inst, release, disks[name]), stdin=f)
        except (CommandError, OSError, KeyboardInterrupt) as e:
            log(str(e) or "interrupted")
            code = 1
        if code == 0:
            inst.mailbox = ""   # the other server's now: a remove here must not delete it
            reg.save()
            instances.disable(s, reg, name)
        elif remote(to, receive_args(inst, release, disks[name], check=True)) == 0:   # it did not get there
            run(["incus", "config", "unset", name, "boot.autostart"])
            run(["incus", "start", name])
            raise CommandError(f"moving {name} failed; it runs here again")
        else:
            raise CommandError(
                f"moving {name} failed, and it may have reached {to}; it stays stopped here. On {to}:\n"
                f"  - if thinny instance list shows {name}, finish it there if pending (thinny instance add {name}), "
                f"then here: thinny instance disable {name}\n"
                f"  - if not, delete any container {name} there and its pool (incus config device get {name} root pool "
                f"before incus delete {name}; without a container, the pool the check above names), then here: "
                f"incus config unset {name} boot.autostart && incus start {name}" +
                (f"\nif {name} is there, its mailbox {inst.mailbox} is that server's: remove {name} here with "
                 f"thinny instance remove {name} --keep-mailbox" if inst.mailbox and not inst.own_mailbox else ""))


def receive_args(inst: Instance, release: tuple[str, str], disk_gb: int, check: bool = False) -> list[str]:
    """The other server's `thinny instance receive` for the instance, whose disk is `disk_gb` GiB (or in a check,
    the disks that server would get with it)."""
    return ["instance", "receive", inst.name, "--access", inst.access,
            *(["--url", inst.url] if inst.access == "url" else []),
            *(["--frontend", inst.frontend] if inst.frontend != "webapp" else []),   # an older thinny knows none
            *(["--mailbox", inst.mailbox] if inst.mailbox else []),   # nor this: it would not delete the mailbox
            *(["--own-mailbox"] if inst.mailbox and inst.own_mailbox else []),   # nor this: it would delete it
            "--release", *release, "--built-ins", inst.built_ins, "--disk-gb", str(disk_gb),
            *(["--check"] if check else [])]


def remote(to: str, args: list[str], stdin: BinaryIO | None = None) -> int:
    """The exit code of thinny `args` on the server `to` (255 when SSH fails), run over SSH as root or with
    sudo without a password: `stdin` may carry an archive, so nothing could answer a prompt."""
    command = shlex.join(["sh", "-c", 'if [ "$(id -u)" = 0 ]; then exec "$@"; fi; exec sudo -n "$@"', "sh",
                          host.THINNY, *args])
    # Not system.run: the other thinny is trusted, and its output belongs on the admin's terminal.
    # A dead connection ends with 255; no time limit, a large import may take long.
    return subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
                           "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", "--", to, command],
                          stdin=stdin or subprocess.DEVNULL).returncode


def receive(s: Settings, reg: Registry, name: str, access: str, url: str, release: tuple[str, str], built_ins: str,
            disk_gb: int, archive: BinaryIO | None, frontend: str = "webapp", mailbox: str = "",
            own_mailbox: bool = False) -> None:
    """Take an instance another server moves here: `archive` is its `incus export`, which comes with
    the Hermes `release` and `built_ins` it has, the size of its disk, and its `mailbox`, which thinny created for
    it unless `own_mailbox`.
    Without an archive, only check that it could (with room for `disk_gb` GiB of disks)."""
    if name in reg.instances:
        raise CommandError(f"{name} exists on this server too; names are not reused")
    instances.check_free_name(name)
    instances.check_access(s, reg, name, access, url)
    here = host.release()
    if here not in (None, release):
        raise CommandError(f"{name} runs Hermes {release[0]}, this server {here[0]}, and Hermes databases cannot be "
                           "downgraded; run thinny update --all on both servers first")
    if update.UNFINISHED.exists():
        raise CommandError("an update is unfinished on this server; run thinny update --all on both servers first")
    reg.next_port(s.port_range)   # raise when none is left
    reg.next_address()
    if archive is None:
        instances.check_room(name, disk_gb)
        return

    path = STATE_DIR / f"{name}.tar.gz"
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            shutil.copyfileobj(archive, f)
        log(f"{name}: importing its container")
        host.write_policy(s, name)   # Incus refuses the container while its policy device's source is missing
        # Before registering: a failed import leaves only that policy folder here, which a retry reuses and uninstall
        # deletes, so the move can be tried again.
        instances.create_pool(name, disk_gb)
        try:
            run(["incus", "import", str(path), name, "--storage", instances.pool_of(name), "--quiet"],
                timeout=ARCHIVE_TIMEOUT)
        except CommandError:
            instances.delete_pool(name)
            raise
    finally:
        path.unlink(missing_ok=True)
    inst = reg.add(name, access, s.port_range)
    inst.moved, inst.built_ins, inst.frontend, inst.mailbox, inst.own_mailbox = \
        True, built_ins, frontend, mailbox, own_mailbox
    reg.save()
    if here is None:
        host.write_release(*release)
    instances.add(s, reg, name, access, url, just_imported=True)
