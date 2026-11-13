import json
import subprocess

import pytest

from hermes_manager import host, render, system
from hermes_manager.settings import Settings

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
    monkeypatch.setattr(host, "FIREWALL_FILE", tmp_path / "hermes-manager.nft")
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
    assert [c for c, _ in calls] == ["incus profile show hm-person", "incus profile create hm-person",
                                     "incus profile edit hm-person"]
    assert json.loads(calls[-1][1])["config"]["limits.memory"] == "2048MiB"
    missing.discard("profile")
    calls.clear()
    host.ensure_profile(S)
    assert [c for c, _ in calls] == ["incus profile show hm-person", "incus profile edit hm-person"]


def test_the_firewall_is_checked_then_loaded(commands):
    calls, _ = commands
    host.firewall(S)
    path = host.FIREWALL_FILE
    assert 'oifname "ens3" ip saddr 10.78.0.0/24 masquerade' in path.read_text()
    assert [c for c, _ in calls] == [f"nft -c -f {path}", f"nft -f {path}"]


def test_units_and_forwarding(commands):
    calls, _ = commands
    host.install_units(S)
    assert (host.SYSTEMD / "incus.service.d/hermes-manager.conf").read_text() == render.INCUS_DROPIN
    assert "net.ipv4.ip_forward = 1" in host.SYSCTL_FILE.read_text()
    assert "-kernel.apparmor_restrict_unprivileged_userns = 0" in host.SYSCTL_FILE.read_text()
    assert [c for c, _ in calls] == ["systemctl daemon-reload", "systemctl enable --now hermes-manager-update.timer",
                                     f"/usr/lib/systemd/systemd-sysctl {host.SYSCTL_FILE}"]
