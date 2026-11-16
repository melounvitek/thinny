"""An instance's Hermes data for its users, e.g. when someone leaves: `hermes backup` in its container, copied to a
file only root reads, without the files that hold sign-ins or passwords."""

from __future__ import annotations

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
            print("left out:", info.filename, file=sys.stderr)
        elif not info.is_dir():
            kept = zipfile.ZipInfo(info.filename, info.date_time)
            kept.external_attr, kept.file_size = info.external_attr, info.file_size   # its mode; the size for zip64
            kept.compress_type = zipfile.ZIP_DEFLATED
            with source.open(info) as f, copy.open(kept, "w") as g:
                shutil.copyfileobj(f, g, 1 << 20)
'''
