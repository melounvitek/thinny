"""Site settings from /etc/thinny/site.toml."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

SITE_TOML = Path("/etc/thinny/site.toml")
ACCESS_METHODS = ("lan", "tailscale", "url", "local")


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    model: str
    allowed_models: tuple[str, ...] = ()
    model_lock: bool = False
    model_provider: str = "openai-codex"
    reasoning_effort: str = "high"
    approvals: str = "smart"
    language: str = "en"
    language_pack: str = ""
    model_guard_message: str = "Model {model} is not allowed on this server. Allowed: {allowed}."
    auto_update: bool = True
    update_time: str = "03:00"
    min_release_age_hours: float = 48
    container_cpu: int = 2
    container_memory_mb: int = 4096
    container_disk_gb: int = 30
    port_range: tuple[int, int] = (9120, 9199)
    theme: str = "light"
    text_scale: int = 110
    interface: str = "simple"
    known_models: tuple[str, ...] = ()
    access_default: str = "lan"
    lan_address: str = ""
    network_interface: str = "eth0"
    blocked_networks: tuple[str, ...] = ()


# (section, key) in site.toml -> Settings field
KEYS = {
    ("hermes", "auto_update"): "auto_update",
    ("hermes", "update_time"): "update_time",
    ("hermes", "min_release_age_hours"): "min_release_age_hours",
    ("policy", "model_provider"): "model_provider",
    ("policy", "model"): "model",
    ("policy", "allowed_models"): "allowed_models",
    ("policy", "model_lock"): "model_lock",
    ("policy", "reasoning_effort"): "reasoning_effort",
    ("policy", "approvals"): "approvals",
    ("policy", "language"): "language",
    ("policy", "language_pack"): "language_pack",
    ("policy", "model_guard_message"): "model_guard_message",
    ("container", "cpu"): "container_cpu",
    ("container", "memory_mb"): "container_memory_mb",
    ("container", "disk_gb"): "container_disk_gb",
    ("webapp", "port_range"): "port_range",
    ("webapp", "theme"): "theme",
    ("webapp", "text_scale"): "text_scale",
    ("webapp", "interface"): "interface",
    ("webapp", "known_models"): "known_models",
    ("access", "default"): "access_default",
    ("access", "lan_address"): "lan_address",
    ("network", "interface"): "network_interface",
    ("network", "blocked_networks"): "blocked_networks",
}


def load(path: Path = SITE_TOML) -> Settings:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SettingsError(f"cannot read {path}: {e.strerror}") from e
    return parse(text, source=str(path))


def parse(text: str, source: str = "site.toml") -> Settings:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{source}: {e}") from e

    values = {}
    for section, table in data.items():
        if not isinstance(table, dict):
            raise SettingsError(f"{source}: [{section}] must be a section")
        for key, value in table.items():
            name = KEYS.get((section, key))
            if name is None:
                raise SettingsError(f"{source}: unknown setting {section}.{key}")
            values[name] = value

    types = {f.name: f.type for f in fields(Settings)}
    for name, value in values.items():
        values[name] = _convert(name, types[name], value, source)
    if "model" not in values:
        raise SettingsError(f"{source}: policy.model is required")
    settings = Settings(**values)
    _validate(settings, source)
    return settings


def _convert(name, type_name, value, source):
    if type_name.startswith("tuple"):
        item_type = "int" if type_name == "tuple[int, int]" else "str"
        if not isinstance(value, list) or not all(_is(item, item_type) for item in value):
            raise SettingsError(f"{source}: {name} must be a list of {item_type}")
        return tuple(value)
    if not _is(value, type_name):
        raise SettingsError(f"{source}: {name} must be {type_name}, not {value!r}")
    return value


def _is(value, type_name):
    if isinstance(value, bool):
        return type_name == "bool"
    expected = {"bool": bool, "str": str, "int": int, "float": (int, float)}[type_name]
    return isinstance(value, expected)


def _validate(s: Settings, source: str) -> None:
    def fail(message):
        raise SettingsError(f"{source}: {message}")

    for model in (s.model, *s.allowed_models):
        if not model or "<" in model:
            fail("set policy.model and policy.allowed_models to real model ids")
    if s.model_lock and s.model not in s.allowed_models:
        fail("with policy.model_lock, policy.model must be one of policy.allowed_models")
    if s.approvals not in ("manual", "smart"):
        fail("policy.approvals must be manual or smart (off runs every command and computer_use unasked)")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s.update_time):
        fail("hermes.update_time must be HH:MM")
    if len(s.port_range) != 2 or not 1024 <= s.port_range[0] < s.port_range[1] <= 65535:
        fail("webapp.port_range must be [low, high] between 1024 and 65535")
    for name, value in (("cpu", s.container_cpu), ("memory_mb", s.container_memory_mb),
                        ("disk_gb", s.container_disk_gb)):
        if value < 1:
            fail(f"container.{name} must be at least 1")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", s.network_interface):
        fail("network.interface must be an interface name, e.g. eth0")
    if s.theme not in ("light", "dark"):
        fail("webapp.theme must be light or dark")
    if s.access_default not in ACCESS_METHODS:
        fail(f"access.default must be one of {', '.join(ACCESS_METHODS)}")
    if s.lan_address:
        try:
            ipaddress.ip_address(s.lan_address)
        except ValueError:
            fail("access.lan_address must be an IP address")
    elif s.access_default == "lan":
        fail("access.lan_address is required when access.default is lan")
    for network in s.blocked_networks:
        try:
            ipaddress.ip_network(network)
        except ValueError:
            fail(f"network.blocked_networks: {network!r} is not a network")
