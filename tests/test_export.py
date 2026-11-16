import io
import os
import subprocess
import sys
import zipfile

import pytest

from thinny import export

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
