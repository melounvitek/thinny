import dataclasses
import datetime as dt
import json
import subprocess

import pytest

from thinny import checks, host, instances, render, system
from thinny.registry import Instance, Registry
from thinny.settings import Settings

S = Settings(model="m1", allowed_models=("m1",), lan_address="192.168.1.10")
NOW = dt.datetime(2026, 11, 12, 9, 0, tzinfo=dt.UTC)
BINARY = "/home/alice/.hermes/hermes-agent/.hermes/bin/hermes"


# `nft list table inet thinny` on Debian 13 after loading render.firewall("eth0", ["203.0.113.0/24"])
LISTED = """table inet thinny {
	set blocked_v4 {
		type ipv4_addr
		flags interval
		elements = { 0.0.0.0/8, 10.0.0.0/8,
			     100.64.0.0/10, 127.0.0.0/8,
			     169.254.0.0/16, 172.16.0.0/12,
			     192.168.0.0/16, 203.0.113.0/24,
			     224.0.0.0/3 }
	}

	chain forward {
		type filter hook forward priority filter - 1; policy accept;
		iifname "thinny-*" oifname != "eth0" drop
		iifname "thinny-*" ip daddr @blocked_v4 drop
		iifname "thinny-*" meta nfproto ipv6 drop
		oifname "thinny-*" ct state != { established, related } drop
	}

	chain input {
		type filter hook input priority filter - 1; policy accept;
		iifname "thinny-*" ct state != { established, related } drop
	}

	chain postrouting {
		type nat hook postrouting priority srcnat; policy accept;
		oifname "eth0" ip saddr 10.78.0.0/24 masquerade
	}
}
"""


INPUT_DROP = '\t\tiifname "thinny-*" ct state != { established, related } drop\n'


@pytest.mark.parametrize("listed, matches", [
    (LISTED, True),
    (LISTED[LISTED.index("\tchain postrouting"):], False),                    # NAT only
    (LISTED.replace(INPUT_DROP, '\t\tiifname "thinny-*" accept\n' + INPUT_DROP), False),  # an early accept
    (LISTED.replace(INPUT_DROP, "").replace("\tchain postrouting", "\tchain spare {\n" + INPUT_DROP
                                             + "\t}\n\tchain postrouting"), False),  # the drop moved away
    (LISTED.replace("203.0.113.0/24", "198.51.100.0/24"), False),            # stale blocked networks
    (LISTED.replace("\tset blocked_v4 {", "\tset blocked_v4 {\n\t\ttype ipv4_addr\n\t}\n\tset spare {"),
     False),                                                                 # the networks in another set
    ("", False),                                                             # not loaded
])
def test_the_loaded_firewall_must_be_the_rendered_one(listed, matches):
    rendered = render.firewall("eth0", ["203.0.113.0/24"])
    assert (checks.table_parts(listed) == checks.table_parts(rendered)) is matches


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


def test_an_own_model_guard_is_a_bypass_only_with_the_model_lock(monkeypatch):
    monkeypatch.setattr(system, "as_user", lambda name, cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(system, "read_user_file", lambda name, path: "")
    alice = Instance("alice", 9120, "local", state="active")
    assert [r[0] for r in checks.bypass(S, alice)] == ["OK"]
    assert [r[0] for r in checks.bypass(dataclasses.replace(S, model_lock=True), alice)] == ["WARN"]


@pytest.mark.parametrize("plugins, has_claude, expected", [
    ({"claude-subscription-directsdk-experimental": "enabled", "hermes-3310": "not enabled"}, True,
     [("OK", "alice: Claude Code and the built-in plugins installed")]),   # turning one off is their choice
    ({"claude-subscription-directsdk-experimental": "enabled"}, False,
     [("WARN", "alice: Claude Code is missing (the Claude subscription plugin runs it)"),
      ("WARN", "alice: plugin hermes-3310 is missing")]),
])
def test_missing_built_ins_are_reported(monkeypatch, plugins, has_claude, expected):
    monkeypatch.setattr(instances, "plugin_states", lambda name: plugins)
    monkeypatch.setattr(system, "as_user", lambda name, cmd, **kw: subprocess.CompletedProcess(
        cmd, 0 if has_claude else 127, "", ""))
    results = list(checks.built_ins(S, Instance("alice", 9120, "local", state="active")))
    assert [(level, message) for level, message, _ in results] == expected
    assert all(fix.startswith("incus exec alice -- runuser -l alice -c ") for level, _, fix in results
               if level == "WARN")


@pytest.mark.parametrize("reboot, warned", [("02:00", True), ("03:00", True), ("04:30", False), ("now", False)])
def test_automatic_reboot_must_come_after_the_update(reboot, warned):
    dump = ('APT::Periodic::Unattended-Upgrade "1";\nUnattended-Upgrade::Automatic-Reboot "true";\n'
            f'Unattended-Upgrade::Automatic-Reboot-Time "{reboot}";\n')
    levels = [level for level, _, _ in checks.unattended_upgrades(dump, "03:00")]
    assert levels == (["OK", "WARN"] if warned else ["OK"])


def test_unattended_upgrades_off_warns():
    assert [r[0] for r in checks.unattended_upgrades('APT::Periodic::Unattended-Upgrade "0";\n', "03:00")] == ["WARN"]


@pytest.mark.parametrize("answer, expected", [
    ("404", ("OK", "Caddy answers the tunnel at http://127.0.0.1:8080/")),
    ("000", ("FAIL", "nothing answers the tunnel at http://127.0.0.1:8080/")),
])
def test_caddy_must_answer_the_tunnel_of_url_instances(monkeypatch, answer, expected):
    reg = Registry({"jirka": Instance("jirka", 9120, "url", url="https://jirka.example.org/", state="active")})
    monkeypatch.setattr(system, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "active\n", ""))
    monkeypatch.setattr(system, "http_status", lambda url: answer)
    assert list(checks.caddy(S, reg)) == []   # no tunnel port, no Caddy for url instances
    results = list(checks.caddy(dataclasses.replace(S, tunnel_port=8080), reg))
    assert [(level, message) for level, message, _ in results] == [("OK", "caddy active"), expected]


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


def test_status_is_the_quick_look(tmp_path, monkeypatch, capsys):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active"),
                    "bob": Instance("bob", 9121, "local", state="active"),
                    "carol": Instance("carol", 9122, "local", state="disabled")})
    (tmp_path / "release").write_text("v0.23.1 abc123\n")
    monkeypatch.setattr(host, "RELEASE_FILE", tmp_path / "release")
    monkeypatch.setattr(checks, "UPDATE_STATUS", tmp_path / "update-status.json")
    monkeypatch.setattr(instances, "container_state", lambda name: {"alice": "RUNNING", "bob": "STOPPED"}[name])
    monkeypatch.setattr(system, "gateway_active", lambda name: True)
    assert checks.status(S, reg) == 1
    assert capsys.readouterr().out == ("OK   Hermes release of all instances: v0.23.1\n"
                                       "WARN no nightly update status yet (the update timer has not run)\n"
                                       "OK   alice: running, gateway active\n"
                                       "FAIL bob: container is STOPPED\n"
                                       "     fix: incus start bob\n")
