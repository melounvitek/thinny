"""An instance's Hermes data for its users, e.g. when someone leaves: `hermes backup` in its container, copied to a
file only root reads, without the files that hold sign-ins or passwords."""

from __future__ import annotations

import contextlib
import os
import shutil
import time

from . import instances
from .registry import Registry, RegistryError
from .system import STATE_DIR, CommandError, as_user, printable, run_bytes, step

EXPORTS = STATE_DIR / "exports"
TIMEOUT = 3600       # for each of the backup and its copy: Hermes's database may be large
KEEP_FREE = 0.1      # of the server's disk, which an export never takes

# Run by the instance's Hermes's Python, as its account, with its Hermes home as argv[1]: the archive `hermes backup`
# wrote, on stdout without the files that hold sign-ins or passwords; their names go to stderr. Hermes decides
# which are its own; without its list there is no archive.
WITHOUT_SECRETS = r'''
import os, shutil, sys, zipfile
from pathlib import Path
home = Path(sys.argv[1])
archive = os.fdopen(os.dup(1), "wb")
os.dup2(2, 1)   # what Hermes prints on the way goes to stderr, not into the archive
sys.path.insert(0, str(home / "hermes-agent"))
from agent.file_safety import HOME_CREDENTIAL_DIRS
from hermes_cli.profiles import profile_path_is_private

# What Hermes's own profile export leaves out at any depth too (_EXPORT_CREDENTIAL_FILES in v0.23.1).
ANYWHERE = [tuple(p.split("/")) for p in ("auth.json", ".env", ".envrc", "bot-desktop", *HOME_CREDENTIAL_DIRS)]


def private(member):
    parts = tuple(member.casefold().removeprefix("_external/").split("/"))   # _external/: relative to ~
    if parts[0] == "profiles" and len(parts) > 2:
        parts = parts[2:]   # relative to the profile's home
    return (profile_path_is_private(parts)
            or any(parts[i:i + len(store)] == store for store in ANYWHERE for i in range(len(parts)))
            # WebUI's keys, sign-in sessions and passkeys; the phone line's Asterisk config, with the SIP password
            or parts[0] == "webui" and len(parts) == 2 and (parts[1].startswith(".") or parts[1] == "passkeys.json")
            or parts[:2] == ("phone", "asterisk"))


with archive, zipfile.ZipFile(home.parent / ".thinny-export/hermes.zip") as source, \
        zipfile.ZipFile(archive, "w") as copy:
    for info in source.infolist():
        if private(info.filename):
            print("left out: " + info.filename, file=sys.stderr)
        elif not info.is_dir():
            kept = zipfile.ZipInfo(info.filename, info.date_time)
            kept.external_attr, kept.file_size = info.external_attr, info.file_size   # its mode; the size for zip64
            kept.compress_type = zipfile.ZIP_DEFLATED
            with source.open(info) as f, copy.open(kept, "w") as g:
                shutil.copyfileobj(f, g, 1 << 20)
'''


def export(reg: Registry, name: str) -> None:
    """Export the instance's Hermes data to a file only root reads, without the files that hold sign-ins or passwords.
    Only an active instance: a disabled one's container stays stopped. What comes from the container is untrusted,
    so the host only writes it, within the size of the instance's disk and never into the last tenth of its own."""
    inst = reg.get(name)
    if inst.state != "active":
        raise RegistryError(f"{name} is {inst.state}")
    if instances.container_state(name) != "RUNNING":
        raise CommandError(f"{name}'s container is not running (see thinny doctor)")
    _, size = instances.disk_use(name)
    EXPORTS.mkdir(mode=0o700, exist_ok=True)
    EXPORTS.chmod(0o700)
    total, _, free = shutil.disk_usage(EXPORTS)
    # Hermes makes the archive in the container first, which takes from the same disk (its pool is a file on it)
    # up to its size.
    if free - total * KEEP_FREE < size:
        raise CommandError(f"the server needs {size / 2**30:.1f} GiB free on top of 10% of its disk to export {name} "
                           f"(Hermes makes the archive in its container first); it has "
                           f"{max(0, free - total * KEEP_FREE) / 2**30:.1f} GiB")

    work = f"/home/{name}/.thinny-export"
    path = EXPORTS / f"{name}-{time.strftime('%Y-%m-%d-%H%M%S')}.zip"
    partial = path.with_name(f".{path.name}.partial")
    try:
        with step(f"{name}: Hermes backup"):
            as_user(name, ["sh", "-c", 'rm -rf "$1" && mkdir -m 700 "$1" && exec hermes backup -o "$1/hermes.zip"',
                           "sh", work], timeout=TIMEOUT)
        with step(f"{name}: its copy without sign-ins"), \
                os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as f:
            def write(chunk: bytes) -> None:
                if f.tell() + len(chunk) > size:
                    raise CommandError(f"{name}'s archive is larger than its disk ({size / 2**30:.1f} GiB); "
                                       "it is not kept")
                total, _, free = shutil.disk_usage(EXPORTS)
                if free - len(chunk) < total * KEEP_FREE:
                    raise CommandError(f"the server's disk would have less than 10% free; {name}'s archive is not kept")
                f.write(chunk)
            code, _, err = run_bytes(instances.hermes_python_cmd(name, WITHOUT_SECRETS), into=write, timeout=TIMEOUT)
        said = err.decode(errors="replace").splitlines()
        if code:
            raise CommandError(f"{name}'s archive could not be copied without sign-ins (exit {code})\n" +
                               "\n".join(map(printable, said[-15:])))
        os.replace(partial, path)
    except OSError as e:
        raise CommandError(f"{partial}: {e.strerror}") from None
    finally:
        partial.unlink(missing_ok=True)
        # Its users' own data in their own disk, which the next export removes: whatever fails here is not the result.
        with contextlib.suppress(CommandError):
            as_user(name, ["rm", "-rf", work], check=False)

    left_out = [printable(line.removeprefix("left out: ")) for line in said if line.startswith("left out: ")]
    print(f"{name}: {path} ({path.stat().st_size / 2**20:.1f} MiB), readable by root only")
    if left_out:
        print("Left out, as they hold sign-ins or passwords: " + ", ".join(left_out[:10]) +
              (f" and {len(left_out) - 10} more" if len(left_out) > 10 else ""))
    print(f"Hand it to {name}'s users, then delete it (see README, Instances). They load it into a Hermes of their own "
          f"with: hermes import {path.name}")
