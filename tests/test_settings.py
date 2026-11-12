import json
from pathlib import Path

import pytest

from hermes_manager.settings import Settings, SettingsError, parse

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
    assert s.allowed_models == ("m1",)
    assert s.port_range == (9120, 9199)


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
    ({"policy__model": "m3"}, "one of policy.allowed_models"),
    ({"access__lan_address": None}, "lan_address is required"),
    ({"access__lan_address": "server.lan"}, "must be an IP address"),
])
def test_invalid_settings_are_refused(changes, message):
    with pytest.raises(SettingsError, match=message):
        parse(site(**changes))


def test_lan_address_is_optional_without_lan_default():
    assert parse(site(access__lan_address=None, access__default="tailscale")).lan_address == ""
