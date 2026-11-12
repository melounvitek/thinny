import json

import pytest

from hermes_manager import render
from hermes_manager.settings import Settings

S = Settings(model="m1", allowed_models=("m1", "m2"), lan_address="192.168.1.10", language_pack="hermes-lang-cs")


def test_managed_config_pins_the_policy():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(render.managed_config(S))
    assert config["model"] == {"provider": "openai-codex", "default": "m1"}
    assert config["plugins"] == {"enabled": ["model-guard", "hermes-lang-cs"], "disabled": []}
    assert config["terminal"]["backend"] == "docker"
    assert config["terminal"]["docker_volumes"] == []
    assert config["terminal"]["docker_shared_container_key"] == ""
    assert config["bot_desktop"] == {"placement": "terminal"}
    assert config["approvals"] == {"mode": "smart"}


def test_managed_env_pins_the_sandbox_runtime():
    env = render.managed_env()
    assert "TERMINAL_ENV=docker\n" in env
    assert "HERMES_DOCKER_BINARY=/usr/local/bin/hermes-podman\n" in env


def test_allowed_models_one_per_line():
    lines = [l for l in render.allowed_models(S).splitlines() if not l.startswith("#")]
    assert lines == ["m1", "m2"]


def test_ui_preferences_show_only_allowed_models():
    prefs = render.ui_preferences(S)
    assert json.loads(prefs["hermes.desktop.visible-models"]) == ["openai-codex::m1", "openai-codex::m2"]
    assert json.loads(prefs["hermes.desktop.model-presets"]) == {
        "openai-codex::m1": {"effort": "high", "serviceTier": "normal", "fast": False}}
    assert prefs["hermes-desktop-mode-v1"] == "light"
    assert all(key.startswith(("hermes.desktop.", "hermes-desktop-")) for key in prefs)


@pytest.mark.parametrize("access, expected", [
    ("lan", "https://192.168.1.10:9123/"),
    ("tailscale", "https://srv.example.ts.net:9123/"),
    ("local", "http://alice.localhost:9123/"),
    ("url", "https://alice.example.org/"),
])
def test_person_url(access, expected):
    assert render.person_url(S, "alice", access, 9123, url="https://alice.example.org/",
                             tailscale_name="srv.example.ts.net") == expected


def test_person_url_brackets_ipv6():
    s = Settings(model="m1", allowed_models=("m1",), lan_address="fd00::5")
    assert render.person_url(s, "alice", "lan", 9120) == "https://[fd00::5]:9120/"


def test_caddyfile_has_one_https_site_per_lan_person_and_no_admin_api():
    text = render.caddyfile(S, [9120, 9122])
    assert "admin off" in text
    assert text.count("tls internal") == 2
    assert "https://192.168.1.10:9122 {" in text
    assert "reverse_proxy 127.0.0.1:9122" in text


def test_firewall_protects_people_whose_slice_exists():
    text = render.firewall(people=[(1001, 9120), (1002, 9121)], slice_uids=[1000, 1001], proxy_uids=[987],
                           blocked_networks=["192.168.1.0/24"], tailscale=False)
    assert 'socket cgroupv2 level 2 "user.slice/user-1001.slice" meta mark != { 0, 987, 1001 } reject' in text
    assert "user-1002.slice" not in text   # no slice yet: nft would refuse the path
    assert "user-1000.slice" not in text   # not a person
    assert "set people { type uid; elements = { 1001, 1002 }; }" in text
    assert "elements = { 192.168.1.0/24 }" in text
    assert "100.64.0.0/10" not in text
    assert text.startswith("# ") and "delete table inet hermes_manager" in text


def test_webapp_ports_only_reach_their_owner():
    text = render.firewall(people=[(1001, 9120), (1002, 9121)], slice_uids=[1001], proxy_uids=[],
                           blocked_networks=[], tailscale=False)
    assert ('iif "lo" tcp dport 9120 ct state new socket cgroupv2 level 2 != "user.slice/user-1001.slice" '
            "reject with tcp reset") in text
    # Without the owner's slice nobody may serve the port.
    assert 'iif "lo" tcp dport 9121 ct state new reject with tcp reset' in text


def test_firewall_blocks_the_tailnet_except_magicdns_when_tailscale_runs():
    text = render.firewall(people=[], slice_uids=[], proxy_uids=[], blocked_networks=[], tailscale=True)
    assert "elements = { 100.64.0.0/10 }" in text
    assert "elements = { fd7a:115c:a1e0::/48 }" in text
    assert text.index("100.100.100.100") < text.index("@blocked_v4 reject")
    assert "set people { type uid; }" in text


def test_firewall_merges_overlapping_networks():
    text = render.firewall(people=[], slice_uids=[], proxy_uids=[], blocked_networks=["fd00::/8"],
                           tailscale=True)
    assert "elements = { fd00::/8 }" in text


def test_update_timer_uses_the_update_time():
    assert "OnCalendar=*-*-* 03:00:00" in render.update_timer(S)
