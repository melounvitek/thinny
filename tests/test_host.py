import dataclasses
import json
import subprocess

import pytest

from thinny import host, render, system
from thinny.registry import Instance, Registry
from thinny.settings import Settings

S = Settings(model="m1", allowed_models=("m1",), access_default="local", network_interface="ens3",
             container_memory_mb=2048)


@pytest.fixture
def commands(tmp_path, monkeypatch):
    """Records host commands; `missing` lists the Incus objects that `show` does not find."""
    calls, missing = [], {"storage", "profile"}

    def run(cmd, *, check=True, input=None, **kw):
        calls.append((" ".join(cmd), input))
        code = 1 if cmd[:2] in (["incus", "storage"], ["incus", "profile"]) and cmd[2] == "show" \
            and cmd[1] in missing else 0
        return subprocess.CompletedProcess(cmd, code, "", "")
    monkeypatch.setattr(host, "run", run)
    monkeypatch.setattr(host, "FIREWALL_FILE", tmp_path / "thinny.nft")
    monkeypatch.setattr(host, "SYSCTL_FILE", tmp_path / "sysctl.conf")
    monkeypatch.setattr(host, "SYSTEMD", tmp_path / "systemd")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    return calls, missing


def test_the_storage_pool_is_created_once(commands):
    calls, missing = commands
    host.init_incus()
    assert any(c.startswith("incus storage create default btrfs size=") for c, _ in calls)
    calls.clear()
    missing.discard("storage")
    host.init_incus()
    assert [c for c, _ in calls] == ["incus storage show default"]


def test_the_profile_follows_the_settings(commands):
    calls, missing = commands
    host.ensure_profile(S)
    assert [c for c, _ in calls] == ["incus profile show thinny-instance", "incus profile create thinny-instance",
                                     "incus profile edit thinny-instance"]
    assert json.loads(calls[-1][1])["config"]["limits.memory"] == "2048MiB"
    missing.discard("profile")
    calls.clear()
    host.ensure_profile(S)
    assert [c for c, _ in calls] == ["incus profile show thinny-instance", "incus profile edit thinny-instance"]


def test_the_firewall_is_checked_then_loaded(commands):
    calls, _ = commands
    host.firewall(S)
    path = host.FIREWALL_FILE
    assert 'oifname "ens3" ip saddr 10.78.0.0/24 masquerade' in path.read_text()
    assert [c for c, _ in calls] == [f"nft -c -f {path}", f"nft -f {path}"]


def test_url_instances_reach_caddy_only_with_the_tunnel_port(commands, tmp_path, monkeypatch):
    calls, _ = commands
    monkeypatch.setattr(host, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setattr(host, "tailscale_status", lambda: None)
    monkeypatch.setattr(host.shutil, "which", lambda name: f"/usr/bin/{name}")
    reg = Registry({"jirka": Instance("jirka", 9121, "url", url="https://jirka.example.org/", state="active")})
    host.reconcile_access(S, reg)
    assert not host.CADDYFILE.exists()
    host.reconcile_access(dataclasses.replace(S, tunnel_port=8080), reg)
    assert "http://jirka.example.org:8080 {" in host.CADDYFILE.read_text()
    assert "systemctl restart caddy" in [c for c, _ in calls]


def test_units_and_forwarding(commands):
    calls, _ = commands
    host.install_units(S)
    assert (host.SYSTEMD / "incus.service.d/thinny.conf").read_text() == render.INCUS_DROPIN
    assert "net.ipv4.ip_forward = 1" in host.SYSCTL_FILE.read_text()
    assert "-kernel.apparmor_restrict_unprivileged_userns = 0" in host.SYSCTL_FILE.read_text()
    assert (host.SYSTEMD / "thinny-trace.service").read_text() == render.TRACE_SERVICE
    assert [c for c, _ in calls] == ["systemctl daemon-reload", "systemctl enable --now thinny-update.timer",
                                     # restarted: a new thinny may have come with install.sh
                                     "systemctl enable thinny-trace.service", "systemctl restart thinny-trace.service",
                                     f"/usr/lib/systemd/systemd-sysctl {host.SYSCTL_FILE}"]


def test_lan_access_copies_caddys_root_certificate_for_any_account(commands, tmp_path, monkeypatch):
    monkeypatch.setattr(host, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setattr(host, "OFFICE_CA", tmp_path / "thinny" / "office-root.crt")
    ca = tmp_path / "caddy" / "root.crt"
    monkeypatch.setattr(host, "caddy_root_ca", lambda: ca)
    monkeypatch.setattr(host, "tailscale_status", lambda: None)
    monkeypatch.setattr(host.shutil, "which", lambda name: f"/usr/bin/{name}")
    reg = Registry({"bob": Instance("bob", 9121, "lan", state="active")})
    lan = dataclasses.replace(S, lan_address="192.168.1.10")
    host.reconcile_access(lan, reg)                 # Caddy has not created its certificate authority yet
    assert not host.OFFICE_CA.exists()
    for cert in ("-----BEGIN CERTIFICATE-----\nfirst\n", "-----BEGIN CERTIFICATE-----\nsecond\n"):
        ca.parent.mkdir(exist_ok=True)
        ca.write_text(cert)
        host.reconcile_access(lan, reg)
        assert host.OFFICE_CA.read_text() == cert
        assert oct(host.OFFICE_CA.stat().st_mode & 0o777) == "0o644"
