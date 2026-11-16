import base64
import dataclasses
import datetime as dt
import json
import os
import subprocess

import pytest

from thinny import checks, dashboard, host, instances, render, system, trace
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


def test_webuis_unit_and_password_file_are_searched_for_bypasses_too():
    unit = f'[Service]\nExecStart="{BINARY}" "gateway" "run"\n'
    webui = "[Service]\nEnvironment=HERMES_MANAGED_DIR=/home/alice/mine\nHERMES_WEBUI_PASSWORD=secret\n"
    results = checks.bypasses("alice", "", unit, own_guard=False, gateway_binary=BINARY, webui_text=webui)
    assert [(level, message) for level, message, _ in results] == [
        ("WARN", "alice: its hermes-webui.service sets HERMES_MANAGED_DIR")]


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


@pytest.mark.parametrize("auth, commit, expected", [
    ({"password_auth_enabled": True}, "d1", [("OK", "alice: WebUI asks for a password"), ("OK", "alice: WebUI w1")]),
    ({"password_auth_enabled": False}, "d1", [("FAIL", "alice: WebUI does not ask for a password"),
                                             ("OK", "alice: WebUI w1")]),
    ({"password_auth_enabled": True}, "d0", [("OK", "alice: WebUI asks for a password"),
                                            ("WARN", "alice: WebUI is not at w1, the release of the instances that "
                                                     "use it (changed by its users or its own updater)")]),
])
def test_a_webui_instance_must_ask_for_a_password_and_run_the_webui_release(monkeypatch, auth, commit, expected):
    monkeypatch.setattr(system, "gateway_active", lambda name: True)
    monkeypatch.setattr(system, "http_status", lambda url: "302")
    monkeypatch.setattr(system, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps(auth), ""))
    monkeypatch.setattr(host, "release", lambda path=None: ("w1", "d1") if path == host.WEBUI_RELEASE_FILE
                        else ("v1", "c1"))
    monkeypatch.setattr(instances, "installed_commit", lambda name, webui=False: commit if webui else "c1")
    alice = Instance("alice", 9120, "local", url="http://alice.localhost:9120/", state="active", frontend="webui")
    results = [*checks.webapp(S, alice), *checks.version(S, alice)]
    assert [(level, message) for level, message, _ in results] == [
        ("OK", "alice: gateway active"), ("OK", "alice: WebUI answers (HTTP 302)"), expected[0],
        ("OK", "alice: Hermes v1"), expected[1]]
    assert {fix for level, _, fix in results if level != "OK"} <= {"thinny instance reset-password alice",
                                                                  "thinny instance frontend alice webui"}


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


@pytest.mark.parametrize("ufw_blocks, fix", [(False, checks.FORWARD_FIX), (True, "ufw route allow in on thinny-+")])
def test_a_container_without_internet_gets_the_ufw_rule_when_ufw_blocks_it(monkeypatch, ufw_blocks, fix):
    monkeypatch.setattr(instances, "container_state", lambda name: "RUNNING")
    monkeypatch.setattr(system, "in_container", lambda name, cmd, **kw: subprocess.CompletedProcess(cmd, 0, "000", ""))
    monkeypatch.setattr(host, "ufw_lacks_forward", lambda: ufw_blocks)
    results = list(checks.container(S, Instance("alice", 9120, "local", state="active")))
    assert results[-1] == ("FAIL", "alice: container cannot reach the internet (HTTP 000)", fix)


@pytest.mark.parametrize("enabled, running, level", [
    ("enabled", "active", "OK"),
    ("enabled", "activating", "WARN"),   # waiting to restart
    ("disabled", "active", "WARN"),      # gone after the next reboot
    ("", "inactive", "WARN"),             # missing
])
def test_doctor_warns_when_admins_access_is_not_traced(monkeypatch, tmp_path, enabled, running, level):
    answers = {"is-enabled": enabled + "\n", "is-active": running + "\n"}
    monkeypatch.setattr(system, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, answers[cmd[1]], ""))
    monkeypatch.setattr(trace, "UNSENT", tmp_path / "unsent")
    [(found, _, fix)] = checks.access_trace(S, Registry())
    assert found == level and fix == ("" if level == "OK" else "thinny config apply")


def test_doctor_warns_while_rows_wait_for_a_sheet(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "enabled\n", ""))
    monkeypatch.setattr(trace, "UNSENT", tmp_path)
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active", trace_sheet="https://script..."),
                    "bob": Instance("bob", 9121, "local", state="active")})   # no sheet: its rows go
    for name in ("alice", "bob"):
        (tmp_path / name).mkdir()
        for row in ("1.json", "2.json"):
            (tmp_path / name / row).write_text("{}")
    results = list(checks.access_trace(S, reg))
    assert results[1:] == [("WARN", "alice: 2 rows of its access trace wait for its Google Sheet",
                            "check that the server reaches script.google.com: the trace tries again every 5 "
                            "minutes, and its journal (journalctl -u thinny-trace) says why they wait")]


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


def test_doctor_shows_what_a_container_said_without_control_characters(monkeypatch, capsys):
    # Its users are root in it: its curl and its Webapp's settings can say anything.
    monkeypatch.setattr(checks, "CHECKS", [])
    monkeypatch.setattr(checks, "INSTANCE_CHECKS", [checks.container, checks.webapp])
    monkeypatch.setattr(instances, "container_state", lambda name: "RUNNING")
    monkeypatch.setattr(system, "in_container",
                        lambda name, cmd, **kw: subprocess.CompletedProcess(cmd, 0, "\x1b]0;owned\x07\x1b[2J", ""))
    monkeypatch.setattr(host, "ufw_lacks_forward", lambda: False)
    monkeypatch.setattr(system, "gateway_active", lambda name: True)
    monkeypatch.setattr(system, "http_status", lambda url: "302")
    monkeypatch.setattr(instances, "webapp_settings", lambda name: {"public_url": "https://\u202eevil.example/\x1b[1A"})
    reg = Registry({"alice": Instance("alice", 9120, "local", url="http://127.0.0.1:9120/", state="active")})
    assert checks.run(S, reg) == 1
    out = capsys.readouterr().out
    assert "FAIL alice: container cannot reach the internet (HTTP ]0;owned[2J)\n" in out
    assert "WARN alice: Webapp public_url is https://evil.example/[1A, not http://127.0.0.1:9120/\n" in out
    assert all(c.isprintable() or c == "\n" for c in out)


def test_status_is_the_quick_look(tmp_path, monkeypatch, capsys):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active"),
                    "bob": Instance("bob", 9121, "local", state="active"),
                    "carol": Instance("carol", 9122, "local", state="disabled")})
    (tmp_path / "release").write_text("v0.23.1 abc123\n")
    (tmp_path / "webui-release").write_text("v0.52.678 def456\n")
    monkeypatch.setattr(host, "RELEASE_FILE", tmp_path / "release")
    monkeypatch.setattr(host, "WEBUI_RELEASE_FILE", tmp_path / "webui-release")
    monkeypatch.setattr(checks, "UPDATE_STATUS", tmp_path / "update-status.json")
    monkeypatch.setattr(instances, "container_state", lambda name: {"alice": "RUNNING", "bob": "STOPPED"}[name])
    monkeypatch.setattr(system, "gateway_active", lambda name: True)
    assert checks.status(S, reg) == 1
    assert capsys.readouterr().out == ("OK   Hermes release of all instances: v0.23.1\n"
                                       "OK   WebUI release of the instances that use it: v0.52.678\n"
                                       "WARN no nightly update status yet (the update timer has not run)\n"
                                       "OK   alice: running, gateway active\n"
                                       "FAIL bob: container is STOPPED\n"
                                       "     fix: incus start bob\n")


def test_doctor_points_office_pcs_at_the_copy_of_caddys_root_certificate(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "active\n", ""))
    ca, copy = tmp_path / "root.crt", tmp_path / "office-root.crt"
    monkeypatch.setattr(host, "caddy_root_ca", lambda: ca)
    monkeypatch.setattr(host, "OFFICE_CA", copy)
    reg = Registry({"bob": Instance("bob", 9121, "lan", state="active")})
    ca.write_text("new")
    for text, level in ((None, "WARN"), ("old", "WARN"), ("new", "OK")):
        if text:
            copy.write_text(text)
        result = list(checks.caddy(S, reg))[-1]
        assert result[0] == level and str(copy) in result[1]
        assert result[2] == ("" if level == "OK" else "thinny config apply")


LISTENING = """\
LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:* users:(("prometheus",pid=11,fd=7))
LISTEN 0 4096 127.0.0.1:9100 0.0.0.0:* users:(("prometheus-node",pid=12,fd=3))
LISTEN 0 4096 127.0.0.1:8444 0.0.0.0:* users:(("incusd",pid=13,fd=21))
LISTEN 0 4096 127.0.0.1:9200 0.0.0.0:* users:(("grafana",pid=14,fd=9))
LISTEN 0 4096 192.168.1.10:9200 0.0.0.0:* users:(("caddy",pid=15,fd=8))
LISTEN 0 4096 100.100.86.53%tailscale0:9200 0.0.0.0:* users:(("tailscaled",pid=16,fd=30))
LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=17,fd=3))
"""


@pytest.fixture
def monitoring(tmp_path, monkeypatch):
    """The dashboard on at 9200 on the LAN, all well: the test changes the commands' `answers`."""
    host.DASHBOARD_STATE.write_text("9200 lan\n")
    monkeypatch.setattr(dashboard, "CERT", tmp_path / "metrics.crt")
    dashboard.CERT.write_text(f"-----BEGIN CERTIFICATE-----\n{base64.b64encode(b'cert').decode()}\n"
                              "-----END CERTIFICATE-----\n")
    monkeypatch.setattr(dashboard, "DISK_METRICS", tmp_path / "thinny.prom")
    dashboard.DISK_METRICS.write_text(render.disk_metrics([("alice", 1, 2)]))
    targets = [{"labels": {"job": job}, "health": "up", "lastError": ""} for job in ("incus", "node")]
    answers = {"systemctl is-active": "active\n" * 4, "ss -Hltnp": LISTENING,
               "incus config get core.metrics_address": "127.0.0.1:8444\n",
               "incus config trust list": json.dumps([{"fingerprint": dashboard.fingerprint(), "type": "metrics"}]),
               "curl -s -m 5 http://127.0.0.1:9090/api/v1/targets": json.dumps({"data": {"activeTargets": targets}})}

    def run(cmd, **kw):
        line = " ".join(cmd)
        return subprocess.CompletedProcess(cmd, 0, next((out for start, out in answers.items()
                                                         if line.startswith(start)), ""), "")
    for module in (system, dashboard):
        monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(system, "http_status", lambda url: "200" if url == "http://127.0.0.1:9200/api/health" else "000")
    return answers


ON = dataclasses.replace(S, dashboard_enabled=True, dashboard_access="lan")


def test_doctor_checks_the_dashboard_only_while_it_is_on(monitoring):
    assert list(checks.dashboard(S, Registry())) == []
    assert [(level, message) for level, message, _ in checks.dashboard(ON, Registry())] == [
        ("OK", "the dashboard's Prometheus, node exporter and Grafana active"),
        ("OK", "the dashboard listens on 127.0.0.1 only, but where Caddy or Tailscale publish it"),
        ("OK", "Incus serves its metrics at 127.0.0.1:8444 to Prometheus's certificate"),
        ("OK", "Prometheus reads Incus's metrics"),
        ("OK", "Prometheus reads the host's and thinny's"),
        ("OK", "the instances' disk use is fresh"),
        ("OK", "Grafana answers (HTTP 200)")]


def test_doctor_says_how_to_install_a_dashboard_that_is_on_but_not_installed(monitoring):
    host.DASHBOARD_STATE.unlink()
    assert list(checks.dashboard(ON, Registry())) == [
        ("WARN", "dashboard.enabled is true, but the dashboard is not installed",
         "thinny config apply (on a terminal: it asks)")]


def test_doctor_finds_what_of_the_dashboard_is_amiss(monitoring):
    monitoring["systemctl is-active"] = "active\ninactive\nactive\nactive\n"
    monitoring["ss -Hltnp"] = LISTENING.replace("127.0.0.1:9090", "0.0.0.0:9090").replace("127.0.0.1:9200", "*:9200")
    monitoring["incus config trust list"] = "[]"
    targets = [{"labels": {"job": "incus"}, "health": "up", "lastError": ""},
               {"labels": {"job": "node"}, "health": "down", "lastError": "connection refused"}]
    monitoring["curl -s -m 5 http://127.0.0.1:9090/api/v1/targets"] = json.dumps({"data": {"activeTargets": targets}})
    old = dt.datetime.now().timestamp() - 20 * 60
    os.utime(dashboard.DISK_METRICS, (old, old))
    results = [(level, message) for level, message, _ in checks.dashboard(
        dataclasses.replace(ON, dashboard_port=9300), Registry())]
    assert results == [
        ("WARN", "the dashboard is not as config.toml says"),
        ("FAIL", "the dashboard's prometheus-node-exporter.service not active"),
        ("FAIL", "the dashboard listens beyond 127.0.0.1: 0.0.0.0:9090, *:9200"),
        ("FAIL", "Incus serves no metrics at 127.0.0.1:8444 to Prometheus's certificate"),
        ("OK", "Prometheus reads Incus's metrics"),
        ("FAIL", "Prometheus does not read the host's and thinny's: connection refused"),
        ("WARN", "the instances' disk use is missing or older than 15 minutes"),
        ("OK", "Grafana answers (HTTP 200)")]
