import json
from pathlib import Path

import pytest

from thinny.settings import Settings, SettingsError, parse

EXAMPLE = Path(__file__).parent.parent / "site.example.toml"


def site(**changes):
    """site.toml text: a minimal valid site with `section__key=value` changes (None removes)."""
    sections = {"policy": {"model": "m1", "allowed_models": ["m1", "m2"]},
                "access": {"lan_address": "192.168.1.10"}}
    for name, value in changes.items():
        section, key = name.split("__")
        if value is None:
            sections[section].pop(key)
        else:
            sections.setdefault(section, {})[key] = value
    return "".join(f"[{section}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items())
                   for section, values in sections.items())


def test_minimal_settings_use_defaults():
    assert parse(site()) == Settings(model="m1", allowed_models=("m1", "m2"), lan_address="192.168.1.10")


def test_example_needs_real_values_before_use():
    with pytest.raises(SettingsError, match="real model ids"):
        parse(EXAMPLE.read_text())


def test_example_parses_once_filled_in():
    text = EXAMPLE.read_text().replace("<model id>", "m1").replace("<server LAN IP>", "10.0.0.5")
    s = parse(text)
    assert s.model_lock is False
    assert s.port_range == (9120, 9199)


def test_model_lock_is_off_by_default_and_then_needs_no_allowed_models():
    s = parse(site(policy__allowed_models=None, policy__model="m3"))
    assert (s.model_lock, s.model, s.allowed_models) == (False, "m3", ())
    assert parse(site(policy__model_lock=True)).model_lock is True


@pytest.mark.parametrize("changes, message", [
    ({"policy__modle": "x"}, "unknown setting policy.modle"),
    ({"hermes__auto_update": "yes"}, "auto_update must be bool"),
    ({"webapp__port_range": [9120]}, "port_range"),
    ({"webapp__port_range": [80, 90]}, "port_range"),
    ({"webapp__port_range": ["a", "b"]}, "port_range must be a list of int"),
    ({"hermes__update_time": "3:00"}, "HH:MM"),
    ({"access__default": "vpn"}, "access.default"),
    ({"network__blocked_networks": ["office"]}, "not a network"),
    ({"policy__approvals": "off"}, "manual or smart"),
    ({"policy__model": "m3", "policy__model_lock": True}, "one of policy.allowed_models"),
    ({"policy__allowed_models": None, "policy__model_lock": True}, "one of policy.allowed_models"),
    ({"policy__model_lock": "yes"}, "model_lock must be bool"),
    ({"policy__model": None}, "policy.model is required"),
    ({"access__lan_address": None}, "lan_address is required"),
    ({"access__lan_address": "server.lan"}, "must be an IP address"),
    ({"access__proxy_users": ["www-data"]}, "unknown setting access.proxy_users"),
    ({"sandbox__image": "img"}, "unknown setting sandbox.image"),
    ({"container__cpu": 0}, "container.cpu must be at least 1"),
    ({"container__disk_gb": 1.5}, "container_disk_gb must be int"),
    ({"network__interface": "eth0; drop"}, "network.interface must be an interface name"),
])
def test_invalid_settings_are_refused(changes, message):
    with pytest.raises(SettingsError, match=message):
        parse(site(**changes))


def test_container_limits_and_webapp_look():
    s = parse(site(container__cpu=4, container__memory_mb=8192, container__disk_gb=50))
    assert (s.container_cpu, s.container_memory_mb, s.container_disk_gb) == (4, 8192, 50)
    assert parse(site()).text_scale == 110


def test_lan_address_is_optional_without_lan_default():
    assert parse(site(access__lan_address=None, access__default="tailscale")).lan_address == ""
