import dataclasses
import json

import pytest

from thinny import render
from thinny.settings import Settings

S = Settings(model="m1", allowed_models=("m1", "m2"), lan_address="192.168.1.10", language_pack="hermes-lang-cs")
LOCKED = dataclasses.replace(S, model_lock=True)


def test_managed_config_pins_the_policy():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(render.managed_config(LOCKED))
    assert config["model"] == {"provider": "openai-codex", "default": "m1"}
    assert config["agent"] == {"reasoning_effort": "high"}
    assert config["plugins"] == {"enabled": ["model-guard", "hermes-lang-cs",
                                             "claude-subscription-directsdk-experimental", "hermes-3310"],
                                 "disabled": []}
    assert config["terminal"] == {"backend": "local"}   # the instance's container is the sandbox
    assert config["bot_desktop"] == {"placement": "terminal"}
    assert config["approvals"] == {"mode": "smart"}


def test_without_the_model_lock_users_choose_their_model_effort_and_plugins():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(render.managed_config(S))
    assert config == {"terminal": {"backend": "local"}, "bot_desktop": {"placement": "terminal"},
                      "security": {"redact_secrets": True}, "approvals": {"mode": "smart"}}


def test_managed_env_keeps_the_agent_in_the_container():
    env = render.managed_env()
    assert "TERMINAL_ENV=local\n" in env
    assert "DOCKER" not in env


def test_allowed_models_one_per_line():
    lines = [l for l in render.allowed_models(S).splitlines() if not l.startswith("#")]
    assert lines == ["m1", "m2"]


def test_ui_preferences_show_only_allowed_models():
    prefs = render.ui_preferences(LOCKED)
    assert json.loads(prefs["hermes.desktop.visible-models"]) == ["openai-codex::m1", "openai-codex::m2"]
    assert json.loads(prefs["hermes.desktop.model-presets"]) == {
        "openai-codex::m1": {"effort": "high", "serviceTier": "normal", "fast": False}}
    assert prefs["hermes-desktop-mode-v1"] == "light"
    assert all(key.startswith(("hermes.desktop.", "hermes-desktop-")) for key in prefs)


def test_ui_preferences_without_the_model_lock_leave_the_model_picker_alone():
    prefs = render.ui_preferences(S)
    assert "hermes.desktop.visible-models" not in prefs and "hermes.desktop.known-models" not in prefs
    assert json.loads(prefs["hermes.desktop.model-presets"]) == {
        "openai-codex::m1": {"effort": "high", "serviceTier": "normal", "fast": False}}


@pytest.mark.parametrize("access, expected", [
    ("lan", "https://192.168.1.10:9123/"),
    ("tailscale", "https://srv.example.ts.net:9123/"),
    ("local", "http://alice.localhost:9123/"),
    ("url", "https://alice.example.org/"),
])
def test_instance_url(access, expected):
    assert render.instance_url(S, "alice", access, 9123, url="https://alice.example.org/",
                               tailscale_name="srv.example.ts.net") == expected


def test_instance_url_brackets_ipv6():
    s = Settings(model="m1", allowed_models=("m1",), lan_address="fd00::5")
    assert render.instance_url(s, "alice", "lan", 9120) == "https://[fd00::5]:9120/"


def test_caddyfile_has_one_https_site_per_lan_instance_and_no_admin_api():
    text = render.caddyfile(S, [9120, 9122])
    assert "admin off" in text
    assert text.count("tls internal") == 2
    assert "https://192.168.1.10:9122 {" in text
    assert "reverse_proxy 127.0.0.1:9122" in text


def test_caddyfile_without_lan_instances_needs_no_address():
    s = Settings(model="m1", allowed_models=("m1",), access_default="local")
    assert "https://" not in render.caddyfile(s, [])


TUNNEL_SITES = """
http://jirka.example.org:8080 {
	bind 127.0.0.1
	reverse_proxy 127.0.0.1:9121 {
		trusted_proxies 127.0.0.1/32 ::1/128
		header_up -X-Forwarded-Prefix
	}
}

http://office.example.org:8080 {
	bind 127.0.0.1
	redir /accounting /accounting/
	handle_path /accounting/* {
		reverse_proxy 127.0.0.1:9122 {
			trusted_proxies 127.0.0.1/32 ::1/128
			header_up X-Forwarded-Prefix /accounting
		}
	}
	redir /lawyers /lawyers/
	handle_path /lawyers/* {
		reverse_proxy 127.0.0.1:9123 {
			trusted_proxies 127.0.0.1/32 ::1/128
			header_up X-Forwarded-Prefix /lawyers
		}
	}
	handle {
		respond 404
	}
}

http://:8080 {
	bind 127.0.0.1
	respond 404
}
"""


def test_caddyfile_routes_url_instances_by_host_and_path_for_the_tunnel():
    s = dataclasses.replace(S, tunnel_port=8080)
    text = render.caddyfile(s, [9120], [("https://jirka.example.org/", 9121),
                                        ("https://office.example.org/accounting/", 9122),
                                        ("https://office.example.org/lawyers", 9123)])
    assert text.endswith(TUNNEL_SITES)
    assert "https://192.168.1.10:9120 {" in text


def test_firewall_lets_containers_reach_the_internet_only():
    text = render.firewall("eth0", ["203.0.113.0/24", "10.1.0.0/16", "fd00::/8"])
    assert text.startswith("# ") and "delete table inet thinny" in text
    assert 'iifname "thinny-*" oifname != "eth0" drop' in text            # no other container, no tailnet
    assert 'iifname "thinny-*" ip daddr @blocked_v4 drop' in text
    assert 'iifname "thinny-*" meta nfproto ipv6 drop' in text
    assert 'oifname "thinny-*" ct state != { established, related } drop' in text
    assert 'chain input {\n\t\ttype filter hook input priority filter - 1; policy accept;\n' \
           '\t\tiifname "thinny-*" ct state != { established, related } drop' in text
    assert 'oifname "eth0" ip saddr 10.78.0.0/24 masquerade' in text
    # private ranges (the tailnet included) and the site's networks, merged where they overlap
    assert ("elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, "
            "172.16.0.0/12, 192.168.0.0/16, 203.0.113.0/24, 224.0.0.0/3 }") in text


def test_incus_starts_only_behind_the_firewall():
    dropin = render.INCUS_DROPIN
    assert "ExecStartPre=/usr/sbin/nft -f /etc/thinny/thinny.nft" in dropin
    assert "After=nftables.service" in dropin and "PartOf=nftables.service" in dropin


def test_container_profile_limits_and_isolation():
    profile = json.loads(render.profile(Settings(model="m1", allowed_models=("m1",), lan_address="10.0.0.5",
                                                 container_cpu=3, container_memory_mb=6144, container_disk_gb=40)))
    assert profile["config"] == {
        "boot.autostart": "true", "limits.cpu": "3", "limits.memory": "6144MiB", "limits.processes": "2000",
        "security.idmap.isolated": "true", "security.nesting": "true", "security.privileged": "false"}
    assert profile["devices"] == {
        "root": {"type": "disk", "path": "/", "pool": "default", "size": "40GiB"},
        "tun": {"type": "unix-char", "source": "/dev/net/tun", "path": "/dev/net/tun"}}


def test_container_network_is_static():
    text = render.network_file("10.78.0.7")
    assert "Address=10.78.0.7/32\n" in text
    assert "Gateway=169.254.0.1\nGatewayOnLink=yes\n" in text
    assert "DNS=1.1.1.1\n" in text


def test_update_timer_uses_the_update_time():
    assert "OnCalendar=*-*-* 03:00:00" in render.update_timer(S)
