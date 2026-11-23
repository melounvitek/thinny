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
    # The agent knows its users are not at its host.
    assert config["agent"] == {"reasoning_effort": "high", "environment_hint": render.SERVER_HINT}
    assert config["plugins"] == {"enabled": ["model-guard", "hermes-lang-cs",
                                             "claude-subscription-directsdk-experimental", "hermes-3310"],
                                 "disabled": []}
    assert config["terminal"] == {"backend": "local"}   # the instance's container is the sandbox
    assert config["bot_desktop"] == {"placement": "terminal"}
    assert config["approvals"] == {"mode": "smart"}
    # Hermes does not borrow Claude Code's login, which could log the Claude plugin out.
    assert config["auth"] == {"adopt_external_logins": False}
    # nor messages users at every restart, such as the nightly update's or a reboot's
    assert config["platforms"] == {platform: {"gateway_restart_notification": False}
                                  for platform in ("email", "phone", "telegram")}


def test_without_the_model_lock_users_choose_their_model_effort_and_plugins():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(render.managed_config(S))
    assert config == {"agent": {"environment_hint": render.SERVER_HINT},
                      "terminal": {"backend": "local"}, "bot_desktop": {"placement": "terminal"},
                      "security": {"redact_secrets": True}, "approvals": {"mode": "smart"},
                      "auth": {"adopt_external_logins": False},
                      "platforms": {platform: {"gateway_restart_notification": False}
                                    for platform in ("email", "phone", "telegram")}}



def test_a_groups_hermes_settings_join_the_policy_under_thinnys():
    yaml = pytest.importorskip("yaml")
    group = {"display": {"language": "cs"}, "terminal": {"timeout": 60, "backend": "docker"},
             "mcp_servers": {"github: work": {"command": "npx", "args": ["-y", "server-github"], "env": {}},
                             "on": {"command": "on-mcp"}, "123": {"url": "https://mcp.example.org/"}},
             "approvals": {"mode": "off"}, "security": False, "model": {"default": "m9"},
             "platforms": {"telegram": {"enabled": True, "gateway_restart_notification": True}},
             "agent": {"environment_hint": "Office hours: 9-17."}}
    config = yaml.safe_load(render.managed_config(dataclasses.replace(LOCKED, hermes=group)))
    assert config["display"] == {"language": "cs"}
    assert config["agent"]["environment_hint"] == f"{render.SERVER_HINT}\n\nOffice hours: 9-17."
    assert config["platforms"]["telegram"] == {"enabled": True, "gateway_restart_notification": False}
    assert config["mcp_servers"] == group["mcp_servers"]   # names YAML would read as no strings too
    # thinny's own win, also over a whole section
    assert config["terminal"] == {"timeout": 60, "backend": "local"}
    assert config["approvals"] == {"mode": "smart"} and config["security"] == {"redact_secrets": True}
    assert config["model"] == {"default": "m1", "provider": "openai-codex"}
    # the group's own settings stay as they are
    assert group["terminal"]["backend"] == "docker" and group["agent"] == {"environment_hint": "Office hours: 9-17."}

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


def test_caddyfile_hands_out_only_the_root_certificate_over_http_on_the_lan_address():
    assert """
http://192.168.1.10 {
	bind 192.168.1.10
	handle /office-root.crt {
		root * /etc/thinny
		file_server
	}
	handle {
		respond 404
	}
}
""" in render.caddyfile(S, [9120])


def test_caddyfile_without_lan_instances_needs_no_address():
    s = Settings(model="m1", allowed_models=("m1",), access_default="local")
    text = render.caddyfile(s, [])
    assert "https://" not in text and "office-root.crt" not in text


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
	reverse_proxy 127.0.0.1:9122 {
		trusted_proxies 127.0.0.1/32 ::1/128
		header_up -X-Forwarded-Prefix
	}
}

http://:8080 {
	bind 127.0.0.1
	respond 404
}
"""


def test_caddyfile_routes_url_instances_by_host_for_the_tunnel():
    s = dataclasses.replace(S, tunnel_port=8080)
    text = render.caddyfile(s, [9120], [("https://jirka.example.org/", 9121), ("https://office.example.org", 9122)])
    assert text.endswith(TUNNEL_SITES)
    assert "https://192.168.1.10:9120 {" in text


def test_firewall_lets_containers_reach_the_internet_only():
    text = render.firewall("eth0", ["203.0.113.0/24", "10.1.0.0/16", "fd00::/8"])
    assert text.startswith("# ") and "delete table inet thinny" in text
    assert 'iifname "thinny-*" oifname != "eth0" drop' in text            # no other container, no tailnet
    assert 'iifname "thinny-*" ip daddr @blocked_v4 drop' in text
    assert 'iifname "thinny-*" meta nfproto ipv6 drop' in text
    assert 'iifname "thinny-*" tcp dport 25 drop' in text                  # no mail straight to mail servers
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


def test_the_firewall_check_runs_every_minute_from_boot_on_and_logs_only_what_it_says():
    # AccuracySec: not up to a minute late
    assert "\nOnBootSec=1min\nOnUnitActiveSec=1min\nAccuracySec=1s\n" in render.FIREWALL_TIMER
    assert "\nWantedBy=timers.target\n" in render.FIREWALL_TIMER
    service = render.FIREWALL_SERVICE
    assert "\nType=oneshot\nExecStart=/usr/local/sbin/thinny firewall-check\n" in service
    # systemd's lines about each start are info; what the command says must pass their filter
    assert "\nLogLevelMax=notice\nSyslogLevel=warning\n" in service


def test_container_profile_limits_and_isolation():
    profile = json.loads(render.profile(Settings(model="m1", allowed_models=("m1",), lan_address="10.0.0.5",
                                                 container_cpu=3, container_memory_mb=6144, container_disk_gb=40)))
    # What the kernel would show every container of the others and the server: the other instances' disks (how many,
    # their names, sizes, use, reads and writes), the server's memory and how busy it is. Reads only, and not logged.
    hidden = profile["config"].pop("raw.apparmor").splitlines()
    assert {"deny /sys/devices/**/block/** r,", "deny /sys/fs/btrfs/ r,", "deny /sys/fs/btrfs/** r,",
            "deny /proc/partitions r,", "deny /proc/diskstats r,", "deny /proc/fs/** r,",
            "deny /sys/devices/system/memory/** r,", "deny /proc/pressure/** r,", "deny /proc/vmstat r,"} <= set(hidden)
    assert all(line.startswith("deny /") and line.endswith(" r,") for line in hidden)
    assert profile["config"] == {
        "boot.autostart": "true", "limits.cpu": "3", "limits.memory": "6144MiB", "limits.processes": "2000",
        "security.idmap.isolated": "true", "security.nesting": "true", "security.privileged": "false"}
    # no root disk: each container's is its own pool's
    assert profile["devices"] == {"tun": {"type": "unix-char", "source": "/dev/net/tun", "path": "/dev/net/tun"}}


def test_container_network_is_static():
    text = render.network_file("10.78.0.7")
    assert "Address=10.78.0.7/32\n" in text
    assert "Gateway=169.254.0.1\nGatewayOnLink=yes\n" in text
    assert "DNS=1.1.1.1\n" in text


def test_update_timer_uses_the_update_time():
    assert "OnCalendar=*-*-* 03:00:00" in render.update_timer(S)


@pytest.mark.parametrize("secure", [True, False])
def test_webui_runs_on_the_instances_port_with_cookies_of_its_own(secure):
    unit = render.webui_unit("alice", 9120, secure)
    assert "Environment=HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=9120 " in unit
    assert "HERMES_WEBUI_COOKIE_NAME=hermes_session_alice HERMES_WEBUI_PROFILE_COOKIE_NAME=hermes_profile_alice" in unit
    assert ("HERMES_WEBUI_SECURE=1" in unit) == secure
    # its password: without the file, it does not start at all (an optional one would start it open)
    assert "\nEnvironmentFile=%h/.config/hermes-webui.env\n" in unit


def test_the_dashboards_services_listen_on_127_0_0_1_only():
    s = dataclasses.replace(S, dashboard_enabled=True, dashboard_access="lan", dashboard_port=9300,
                            dashboard_retention_days=14)
    prometheus = render.prometheus_dropin(s, "/etc/thinny/prometheus.yml")
    assert "ExecStart=\nExecStart=/usr/bin/prometheus --config.file=/etc/thinny/prometheus.yml " \
           "--web.listen-address=127.0.0.1:9090" in prometheus
    assert "--storage.tsdb.retention.time=14d" in prometheus
    assert "--web.listen-address=127.0.0.1:9100" in render.NODE_EXPORTER_DROPIN
    grafana = render.grafana_dropin(s, "/etc/thinny/dashboard.env")
    assert "Environment=GF_SERVER_HTTP_ADDR=127.0.0.1\nEnvironment=GF_SERVER_HTTP_PORT=9300\n" in grafana
    assert "EnvironmentFile=/etc/thinny/dashboard.env\n" in grafana
    # no phone home, whatever Grafana's settings
    assert "IPAddressDeny=any\nIPAddressAllow=localhost\n" in grafana
    assert "Environment=GF_ANALYTICS_REPORTING_ENABLED=false\n" in grafana
    assert "Environment=GF_PLUGINS_PREINSTALL_DISABLED=true\n" in grafana


@pytest.mark.parametrize("access, secure", [("lan", "true"), ("tailscale", "true"), ("local", "false")])
def test_grafanas_cookies_are_secure_except_through_the_ssh_tunnel(access, secure):
    grafana = render.grafana_dropin(dataclasses.replace(S, dashboard_access=access), "/etc/thinny/dashboard.env")
    assert f"Environment=GF_SECURITY_COOKIE_SECURE={secure}\n" in grafana


def test_prometheus_reads_incus_with_the_metrics_certificate():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(render.prometheus_config("/etc/thinny/metrics.crt", "/etc/thinny/metrics.key"))
    incus, node = config["scrape_configs"]
    assert (incus["scheme"], incus["metrics_path"], incus["static_configs"]) == \
        ("https", "/1.0/metrics", [{"targets": ["127.0.0.1:8444"]}])
    assert incus["tls_config"] == {"ca_file": "/var/lib/incus/server.crt", "cert_file": "/etc/thinny/metrics.crt",
                                   "key_file": "/etc/thinny/metrics.key"}
    assert node["static_configs"] == [{"targets": ["127.0.0.1:9100"]}]


def test_disk_metrics_for_the_node_exporter():
    text = render.disk_metrics([("alice", 2_000, 30 * 2**30), ("bob", 5, 10)])
    assert 'thinny_instance_disk_used_bytes{name="alice"} 2000\n' in text
    assert f'thinny_instance_disk_quota_bytes{{name="alice"}} {30 * 2**30}\n' in text
    assert 'thinny_instance_disk_quota_bytes{name="bob"} 10\n' in text
    assert "# TYPE thinny_instance_disk_used_bytes gauge\n" in text and text.endswith("10\n")


def test_subscription_usage_for_the_node_exporter():
    text = render.usage_metrics([("alice", "ChatGPT", "5h", 0.37), ("alice", "ChatGPT", "week", 0.124)],
                                [("alice", "5h", 1, 2_600_000), ("alice", "week", 2, 3_600_000)])
    assert 'thinny_instance_subscription_used_ratio{name="alice",provider="ChatGPT",window="5h"} 0.37\n' in text
    assert 'thinny_instance_subscription_used_ratio{name="alice",provider="ChatGPT",window="week"} 0.124\n' in text
    assert 'thinny_instance_turns{name="alice",window="5h"} 1\n' in text
    assert "# TYPE thinny_instance_subscription_used_ratio gauge\n" in text and text.endswith("} 3600000\n")
    assert render.usage_metrics([], []).count("\n") == 6   # their HELP and TYPE: nobody's usage known
