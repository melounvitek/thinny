import io
import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest
from test_instances import MARK, incus  # noqa: F401 - incus: the fake Incus fixture

from thinny import export, system
from thinny.registry import Instance, Registry, RegistryError
from thinny.system import CommandError, OutputTooLarge

GIB = 2**30

# A stand-in for Hermes v0.23.1: a few of its credential stores, matched as its profile_path_is_private matches them
# (case-folded, anything below a store, copies of auth.json at the root), and a few of the OS's credential folders.
HERMES = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/profiles.py": """
import os
print("a warning on the way")
os.write(1, b"and one below Python\\n")
STORES = [("auth.json",), (".env",), ("mcp-tokens",), ("webapp",), ("webhook_subscriptions.json",)]
def profile_path_is_private(parts):
    parts = tuple(p.casefold() for p in parts)
    return (len(parts) == 1 and parts[0].startswith("auth.json.")) or any(parts[:len(s)] == s for s in STORES)
""",
    "agent/__init__.py": "",
    "agent/file_safety.py": 'HOME_CREDENTIAL_DIRS = (".ssh", ".aws", ".config/gh")\n',
}
KEPT = ["config.yaml", "state.db", "memories/MEMORY.md", "skills/example/SKILL.md", "skills/example/authors.json",
        "workspace/.config/other/settings.json", "webui/settings.json", "webui/sessions/abc.json", "cache/images/a.png",
        "profiles/work/config.yaml", "profiles/work/skills/x/SKILL.md", "_external/.honcho/data.json"]
LEFT_OUT = [
    # Hermes's own stores, at the root of a profile, a named one's too, and copies of them
    ".env", "auth.json", "auth.json.bak", "Webhook_Subscriptions.json", "mcp-tokens/github.json", "webapp/session-token",
    "profiles/work/.env", "profiles/work/auth.json",
    # the names it drops at any depth, and the OS's credential folders
    "workspace/.env", "skills/example/auth.json", "skills/example/.ENV", "workspace/project/.ssh/id_ed25519",
    "workspace/.config/gh/hosts.yml", "_external/.ssh/id_rsa",
    # WebUI's keys, sign-in sessions and passkeys; the phone line's Asterisk config
    "webui/.signing_key", "webui/.pbkdf2_key", "webui/.sessions.json", "webui/passkeys.json",
    "phone/asterisk/pjsip.conf", "phone/asterisk/extensions.conf",
]


def hermes_backup(tmp_path, members):
    """An instance's home in tmp_path with its Hermes, and the archive `hermes backup` made of its Hermes home."""
    for path, text in HERMES.items():
        (tmp_path / ".hermes/hermes-agent" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".hermes/hermes-agent" / path).write_text(text)
    (tmp_path / ".thinny-export").mkdir()
    with zipfile.ZipFile(tmp_path / ".thinny-export/hermes.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for i, member in enumerate(members):
            info = zipfile.ZipInfo(member, (2026, 11, 1 + i % 28, 10, 20, 30))
            info.external_attr = (0o600 if i % 2 else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, os.urandom(70_000) if member == "state.db" else f"{member}\n" * 100)
    return tmp_path / ".thinny-export/hermes.zip"


def without_secrets(tmp_path):
    """What the instance's Hermes's Python makes of the archive: stdout, the names it left out, everything on stderr."""
    res = subprocess.run([sys.executable, "-I", "-c", export.WITHOUT_SECRETS, str(tmp_path / ".hermes")],
                         capture_output=True)
    lines = res.stderr.decode().splitlines()
    return res, [line.removeprefix("left out: ") for line in lines if line.startswith("left out: ")], lines


def test_the_archive_leaves_out_what_holds_sign_ins_or_passwords(tmp_path):
    source = hermes_backup(tmp_path, KEPT + LEFT_OUT)
    res, left_out, stderr = without_secrets(tmp_path)
    assert res.returncode == 0, stderr
    assert sorted(left_out) == sorted(LEFT_OUT)
    assert res.stdout.startswith(b"PK\x03\x04")   # Hermes's prints went elsewhere
    assert "a warning on the way" in stderr and "and one below Python" in stderr
    copy = zipfile.ZipFile(io.BytesIO(res.stdout))
    assert copy.testzip() is None   # every member's CRC, as hermes import checks it
    with zipfile.ZipFile(source) as original:
        assert [(i.filename, i.date_time, i.external_attr) for i in copy.infolist()] == \
               [(i.filename, i.date_time, i.external_attr) for i in original.infolist() if i.filename in KEPT]
        assert all(copy.read(name) == original.read(name) for name in KEPT)


def test_without_hermess_list_of_credentials_there_is_no_archive(tmp_path):
    hermes_backup(tmp_path, KEPT + LEFT_OUT)
    (tmp_path / ".hermes/hermes-agent/hermes_cli/profiles.py").write_text("")   # a Hermes that moved it
    res, _, stderr = without_secrets(tmp_path)
    assert res.returncode != 0 and res.stdout == b""
    assert any("profile_path_is_private" in line for line in stderr)


class Disk:
    """The server's disk, as shutil.disk_usage sees it: 100 GiB with `free`. What the export writes takes from it."""

    def __init__(self, free):
        self.total, self.free = 100 * GIB, free

    def usage(self, path):
        assert path == export.EXPORTS
        return shutil._ntuple_diskusage(self.total, self.total - self.free, self.free)


ARCHIVE = os.urandom(300_000)   # what alice's container sends as its archive: the host does not look into it


@pytest.fixture
def alice(incus, tmp_path, monkeypatch):
    """alice, active, with a 30 GiB disk quota and a running container, on a server with 50 of 100 GiB free. Its
    Hermes's backup takes `backup_takes` from the server's disk; the copy without secrets sends `archive` in pieces of
    64 KiB, while other containers take `others_take` for each, exits with `code`, and says `stderr`, its users' and
    Hermes's words."""
    container = {"state": "RUNNING", "devices": {}, "account": True, "hermes": True, "unit": True, "quota": 30 * GIB,
                 "backup_takes": 0, "backup_code": 0, "archive": ARCHIVE, "others_take": 0, "code": 0,
                 "cleanup": None,   # what removing its folder raises (its users' rm may hang or talk)
                 "stderr": b"left out: .env\nleft out: auth.json\nleft out: mcp-tokens/github.json\n"}
    incus.containers["alice"] = container
    disk = Disk(50 * GIB)
    monkeypatch.setattr(shutil, "disk_usage", disk.usage)
    monkeypatch.setattr(export, "EXPORTS", tmp_path / "exports")
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active")})

    def run(cmd, **kw):
        if cmd[:3] == ["incus", "query", "/1.0/instances/alice/state"]:
            incus.calls.append(" ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"disk": {"root": {"total": container["quota"]}}}), "")
        if cmd[-3:] == ["rm", "-rf", "/home/alice/.thinny-export"] and container["cleanup"]:
            incus.calls.append(" ".join(cmd))
            raise container["cleanup"]
        if any("hermes backup" in arg for arg in cmd):
            disk.free -= container["backup_takes"]
            if container["backup_code"]:
                incus.calls.append(" ".join(cmd))
                raise CommandError("hermes backup -> exit 1")
        return incus.run(cmd, **kw)

    def run_bytes(cmd, *, into, timeout):
        assert export.WITHOUT_SECRETS in cmd
        incus.run(cmd, check=False)   # as alice's account, with thinny's mark
        for i in range(0, len(container["archive"]), 65536):
            chunk = container["archive"][i:i + 65536]
            into(chunk)
            disk.free -= len(chunk) + container["others_take"]
        return container["code"], b"", container["stderr"]
    monkeypatch.setattr(export, "run", run)
    monkeypatch.setattr(export, "as_user", lambda name, cmd, **kw: run(system.as_user_cmd(name, cmd), **kw))
    monkeypatch.setattr(export, "run_bytes", run_bytes)
    return reg, container, disk


def kept(tmp_path):
    return sorted(os.listdir(tmp_path / "exports")) if (tmp_path / "exports").exists() else []


def cleaned_up(incus):
    """Its folder in alice's home is gone, after everything else in the container."""
    in_container = [call for call in incus.calls if call.startswith("incus exec alice")]
    return in_container[-1].endswith("rm -rf /home/alice/.thinny-export")


def test_an_export_is_hermess_backup_without_sign_ins_in_a_file_only_root_reads(alice, incus, tmp_path, capsys):
    reg, _, _ = alice
    (tmp_path / "exports").mkdir(mode=0o755)   # e.g. made by hand
    export.export(reg, "alice")
    [name] = kept(tmp_path)
    path = tmp_path / "exports" / name
    assert name.startswith("alice-") and name.endswith(".zip")
    assert (path.read_bytes(), path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777) == \
           (ARCHIVE, 0o600, 0o700)
    backup = incus.where("hermes backup")
    assert f"incus exec alice --cwd /home/alice -- {MARK} runuser -u alice -- env -i" in incus.calls[backup]
    assert 'hermes backup -o "$1/hermes.zip"' in incus.calls[backup] and \
           incus.calls[backup].endswith("/home/alice/.thinny-export")
    assert backup < incus.where("profile_path_is_private")   # the copy without secrets
    assert cleaned_up(incus)
    assert capsys.readouterr().out == (
        f"alice: {path} (0.3 MiB), readable by root only\n"
        "Left out, as they hold sign-ins or passwords: .env, auth.json, mcp-tokens/github.json\n"
        "Hand it to alice's users, then delete it (see README, Instances). They load it into a Hermes of their own "
        f"with: hermes import {name}\n")


def test_only_what_the_container_named_reaches_the_terminal_and_without_escape_sequences(alice, capsys):
    reg, container, _ = alice
    container["stderr"] = (b"a warning on the way\n" + b"".join(b"left out: secret-%d\x1b[2J.json\n" % i for i in range(12)))
    export.export(reg, "alice")
    out = capsys.readouterr().out
    assert "\x1b" not in out and "warning" not in out
    assert "Left out, as they hold sign-ins or passwords: secret-0[2J.json, " in out
    assert "secret-9[2J.json and 2 more\n" in out


@pytest.mark.parametrize("state", ["pending", "disabled", "removed"])
def test_only_an_active_instance_is_exported(alice, incus, tmp_path, state):
    reg, _, _ = alice
    reg.get("alice").state = state
    with pytest.raises(RegistryError, match=f"alice is {state}"):
        export.export(reg, "alice")
    assert not incus.ran("incus exec") and kept(tmp_path) == []


def test_an_instance_whose_container_does_not_run_is_not_exported(alice, incus, tmp_path):
    reg, container, _ = alice
    container["state"] = "STOPPED"
    with pytest.raises(CommandError, match="alice's container is not running"):
        export.export(reg, "alice")
    assert not incus.ran("incus exec") and kept(tmp_path) == []


def test_a_server_without_room_for_the_instances_disk_quota_is_refused_before_the_backup(alice, incus, tmp_path):
    reg, _, disk = alice
    disk.free = 39 * GIB   # 10 GiB to keep, 30 for alice's quota
    with pytest.raises(CommandError, match=r"the server needs 30.0 GiB free on top of 10% of its disk to export alice "
                                           r"\(Hermes makes the archive in its container first\); it has 29.0 GiB"):
        export.export(reg, "alice")
    assert not incus.ran("hermes backup") and kept(tmp_path) == []


# Hermes's archive and its copies in the container take from the same disk, as other containers may meanwhile.
@pytest.mark.parametrize("backup_takes, others_take", [(30 * GIB, 0), (0, 30 * GIB)])
def test_an_export_never_leaves_less_than_a_tenth_of_the_servers_disk_free(alice, incus, tmp_path, backup_takes,
                                                                          others_take):
    reg, container, disk = alice
    disk.free = 40 * GIB + 100_000   # the tenth to keep and alice's quota, and a little
    container["backup_takes"], container["others_take"] = backup_takes, others_take
    with pytest.raises(CommandError, match="the server's disk would have less than 10% free; alice's archive is not "
                                           "kept"):
        export.export(reg, "alice")
    assert kept(tmp_path) == [] and cleaned_up(incus)


def test_an_archive_larger_than_the_instances_disk_quota_is_not_kept(alice, incus, tmp_path):
    reg, container, _ = alice
    container["quota"] = 200_000
    with pytest.raises(CommandError, match=r"alice's archive is larger than its disk quota \(0.0 GiB\); it is not kept"):
        export.export(reg, "alice")
    assert kept(tmp_path) == [] and cleaned_up(incus)


def test_an_export_whose_folder_in_the_container_stays_is_still_handed_over(alice, incus, tmp_path, capsys):
    reg, container, _ = alice
    container["cleanup"] = OutputTooLarge("rm: more than 4000000 bytes of output")
    export.export(reg, "alice")
    [name] = kept(tmp_path)
    assert f"hermes import {name}" in capsys.readouterr().out
    assert cleaned_up(incus)   # tried


def test_a_failed_copy_is_what_the_admin_hears_about_also_when_its_folder_stays(alice, incus, tmp_path):
    reg, container, _ = alice
    container["code"], container["cleanup"] = 1, CommandError("rm: timed out after 600 s")
    with pytest.raises(CommandError, match="could not be copied without sign-ins"):
        export.export(reg, "alice")
    assert kept(tmp_path) == []


def test_a_failed_backup_keeps_nothing(alice, incus, tmp_path):
    reg, container, _ = alice
    container["backup_code"] = 1
    with pytest.raises(CommandError, match="hermes backup"):
        export.export(reg, "alice")
    assert kept(tmp_path) == [] and cleaned_up(incus)


def test_a_failed_copy_keeps_nothing_and_shows_the_end_of_what_it_said_without_escape_sequences(alice, incus, tmp_path):
    reg, container, _ = alice
    container["code"], container["stderr"] = 1, b"Traceback ...\n\x1b]0;owned\x07ImportError: no profile_path_is_private\n"
    with pytest.raises(CommandError) as error:
        export.export(reg, "alice")
    assert str(error.value) == ("alice's archive could not be copied without sign-ins (exit 1)\nTraceback ...\n"
                                "]0;ownedImportError: no profile_path_is_private")
    assert kept(tmp_path) == [] and cleaned_up(incus)
