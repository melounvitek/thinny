import datetime as dt
import json

import pytest

from hermes_manager import checks
from hermes_manager.registry import Registry
from hermes_manager.settings import Settings

S = Settings(model="m1", allowed_models=("m1",), lan_address="192.168.1.10")
NOW = dt.datetime(2026, 11, 12, 9, 0, tzinfo=dt.UTC)
BINARY = "/home/alice/.hermes/hermes-agent/.hermes/bin/hermes"


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

    results = checks.bypasses("alice", env, unit, own_guard=True, gateway_binary=BINARY)
    assert {level for level, _, _ in results} == {"WARN"}
    messages = [message for _, message, _ in results]
    assert messages[0].endswith(".env sets HERMES_MANAGED_DIR, HERMES_ENABLE_PROJECT_PLUGINS")
    assert "model guard" in messages[1]
    assert messages[2].endswith("hermes-gateway.service sets HERMES_BUNDLED_PLUGINS")
    assert f"does not start {BINARY}" in messages[3]
    assert "secret" not in "".join(m + f for _, m, f in results)
    assert "/home/alice/mine" not in "".join(m + f for _, m, f in results)


def test_untouched_home_has_no_bypass():
    unit = f'[Service]\nExecStart="{BINARY}" "gateway" "run"\n'   # as `hermes gateway install` writes it
    results = checks.bypasses("alice", "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=alice\n", unit, own_guard=False,
                              gateway_binary=BINARY)
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
