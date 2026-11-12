import datetime as dt
import json

import pytest

from hermes_manager import checks
from hermes_manager.registry import Registry
from hermes_manager.settings import Settings

S = Settings(model="m1", allowed_models=("m1",), lan_address="192.168.1.10")
NOW = dt.datetime(2026, 11, 12, 9, 0, tzinfo=dt.UTC)


def test_listeners_outside_loopback():
    ss = """\
LISTEN 0      4096       127.0.0.1:9120       0.0.0.0:*    users:(("hermes",pid=1234,fd=12))
LISTEN 0      4096   127.0.0.53%lo:53         0.0.0.0:*    users:(("systemd-resolve",pid=500,fd=15))
LISTEN 0      4096           [::1]:9121          [::]:*    users:(("hermes",pid=1235,fd=12))
LISTEN 0      128          0.0.0.0:8080       0.0.0.0:*    users:(("python3",pid=2001,fd=3),("python3",pid=2002,fd=3))
LISTEN 0      4096            [::]:9999          [::]:*    users:(("node",pid=3001,fd=20))
LISTEN 0      4096               *:7000             *:*    users:(("my app",pid=4001,fd=5))
LISTEN 0      4096   100.64.0.5%tailscale0:443 0.0.0.0:* users:(("tailscaled",pid=600,fd=7))
LISTEN 0      4096   [fd7a:115c:a1e0::5]%tailscale0:443 [::]:* users:(("tailscaled",pid=600,fd=8))
LISTEN 0      64           0.0.0.0:2049       0.0.0.0:*
"""
    assert checks.exposed_listeners(ss) == [
        ("0.0.0.0:8080", "python3", [2001, 2002]),
        ("[::]:9999", "node", [3001]),
        ("*:7000", "my app", [4001]),
        ("100.64.0.5%tailscale0:443", "tailscaled", [600]),
        ("[fd7a:115c:a1e0::5]%tailscale0:443", "tailscaled", [600]),
    ]


@pytest.mark.parametrize("mounts, hidden", [
    ("sysfs /sys sysfs rw 0 0\nproc /proc proc rw,nosuid,nodev,noexec,relatime,hidepid=invisible,gid=998 0 0\n", True),
    ("proc /proc proc rw,relatime,hidepid=2 0 0\n", True),
    ("proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n", False),
    ("proc /proc proc rw,hidepid=invisible 0 0\nproc /proc proc rw,relatime 0 0\n", False),  # the last mount wins
    ("proc /run/sandbox/proc proc rw,hidepid=2 0 0\n", False),
])
def test_hidepid_detection(mounts, hidden):
    assert checks.proc_hidden(mounts) is hidden


@pytest.mark.parametrize("socket_mode, dir_mode, private", [
    (0o140600, 0o40755, True),
    (0o140666, 0o40700, True),
    (0o140666, 0o40755, False),
    (0o140660, 0o40750, False),
])
def test_tailscale_socket_is_private_by_its_mode_or_its_directory(socket_mode, dir_mode, private):
    assert checks.socket_private(socket_mode, dir_mode) is private


@pytest.mark.parametrize("status, level", [
    ({"time": "2026-11-12T03:05:00+00:00", "result": "UPDATED", "detail": "v0.23.1 -> v0.23.2"}, "OK"),
    ({"time": "2026-11-12T03:05:00+00:00", "result": "FAILED", "detail": "alice: gateway not active"}, "FAIL"),
    ({"time": "2026-11-10T03:05:00+00:00", "result": "UPDATED", "detail": ""}, "WARN"),
])
def test_update_status(tmp_path, status, level):
    path = tmp_path / "update-status.json"
    path.write_text(json.dumps(status))
    result = checks.update_status(path, NOW)
    assert result[0] == level
    assert status["result"] in result[1] and status["detail"] in result[1]


def test_missing_update_status_warns(tmp_path):
    assert checks.update_status(tmp_path / "update-status.json", NOW)[0] == "WARN"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_bypasses_are_reported_by_name_only():
    env = ("# HERMES_BUNDLED_PLUGINS=/off\nexport HERMES_MANAGED_DIR=/home/alice/mine\n"
           "HERMES_ENABLE_PROJECT_PLUGINS=1\nOPENAI_API_KEY=sk-secret\n")
    unit = "[Service]\nEnvironment=HERMES_BUNDLED_PLUGINS=/home/alice/p\nExecStart=/home/alice/bin/hermes gateway run\n"

    results = checks.bypasses("alice", env, unit, own_guard=True)
    assert {level for level, _, _ in results} == {"WARN"}
    messages = [message for _, message, _ in results]
    assert messages[0].endswith(".env sets HERMES_MANAGED_DIR, HERMES_ENABLE_PROJECT_PLUGINS")
    assert "model guard" in messages[1]
    assert messages[2].endswith("hermes-gateway.service sets HERMES_BUNDLED_PLUGINS")
    assert "does not start /opt/hermes/hermes-agent/.hermes/bin/hermes" in messages[3]
    assert "secret" not in "".join(m + f for _, m, f in results)
    assert "/home/alice/mine" not in "".join(m + f for _, m, f in results)


def test_untouched_home_has_no_bypass():
    unit = "[Service]\nExecStart=/opt/hermes/hermes-agent/.hermes/bin/hermes gateway run --replace\n"
    results = checks.bypasses("alice", "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=alice\n", unit, own_guard=False)
    assert [level for level, _, _ in results] == ["OK"]


@pytest.mark.parametrize("reboot, warned", [("02:00", True), ("03:00", True), ("04:30", False), ("now", False)])
def test_automatic_reboot_must_come_after_the_update(reboot, warned):
    dump = ('APT::Periodic::Unattended-Upgrade "1";\nUnattended-Upgrade::Automatic-Reboot "true";\n'
            f'Unattended-Upgrade::Automatic-Reboot-Time "{reboot}";\n')
    levels = [level for level, _, _ in checks.unattended_upgrades(dump, "03:00")]
    assert levels == (["OK", "WARN"] if warned else ["OK"])


def test_unattended_upgrades_off_warns():
    assert [r[0] for r in checks.unattended_upgrades('APT::Periodic::Unattended-Upgrade "0";\n', "03:00")] == ["WARN"]


def test_a_crashing_check_fails_without_stopping_the_others(monkeypatch, capsys):
    def broken(s, reg):
        yield checks.ok("before the crash")
        raise RuntimeError("boom")

    def healthy(s, reg):
        yield checks.warn("worth a look", "do this")

    monkeypatch.setattr(checks, "CHECKS", [broken, healthy])
    assert checks.run(S, Registry()) == 1
    assert capsys.readouterr().out == ("OK   before the crash\n"
                                       "FAIL broken crashed: RuntimeError: boom\n"
                                       "WARN worth a look\n"
                                       "     fix: do this\n")

    monkeypatch.setattr(checks, "CHECKS", [healthy])
    assert checks.run(S, Registry()) == 0
