"""Server settings from /etc/thinny/config.toml."""

from __future__ import annotations

import ipaddress
import json
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

CONFIG_TOML = Path("/etc/thinny/config.toml")
ACCESS_METHODS = ("lan", "tailscale", "url", "local")
FRONTENDS = ("webapp", "webui")   # Hermes's built-in Webapp, or hermes-webui
DASHBOARD_ACCESS = ("lan", "tailscale", "local")
# On 127.0.0.1 while the dashboard is on: Incus's metrics, Prometheus, its node exporter.
MONITORING_PORTS = (8444, 9090, 9100)


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    model: str = ""
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
    frontend: str = "webapp"
    theme: str = "light"
    text_scale: int = 110
    interface: str = "simple"
    known_models: tuple[str, ...] = ()
    access_default: str = "lan"
    lan_address: str = ""
    tunnel_port: int = 0
    network_interface: str = "eth0"
    blocked_networks: tuple[str, ...] = ()
    telegram_manager_token: str = ""
    dashboard_enabled: bool = False
    dashboard_access: str = "local"
    dashboard_port: int = 9200
    dashboard_retention_days: int = 30
    groups: dict[str, dict] = field(default_factory=dict)   # name -> the Hermes settings its members' policies pin
    hermes: dict = field(default_factory=dict)   # those of one instance's group (instances.settings_for)


# (section, key) in config.toml -> Settings field
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
    ("webapp", "frontend"): "frontend",
    ("webapp", "theme"): "theme",
    ("webapp", "text_scale"): "text_scale",
    ("webapp", "interface"): "interface",
    ("webapp", "known_models"): "known_models",
    ("access", "default"): "access_default",
    ("access", "lan_address"): "lan_address",
    ("access", "tunnel_port"): "tunnel_port",
    ("network", "interface"): "network_interface",
    ("network", "blocked_networks"): "blocked_networks",
    ("telegram", "manager_token"): "telegram_manager_token",
    ("dashboard", "enabled"): "dashboard_enabled",
    ("dashboard", "access"): "dashboard_access",
    ("dashboard", "port"): "dashboard_port",
    ("dashboard", "retention_days"): "dashboard_retention_days",
}


def load(path: Path = CONFIG_TOML) -> Settings:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SettingsError(f"cannot read {path}: {e.strerror}") from e
    groups = {group.stem: parse_group(group.read_text(encoding="utf-8"), str(group))
              for group in sorted((path.parent / "groups").glob("*.toml"))}
    return replace(parse(text, source=str(path)), groups=groups)


def group_file(name: str) -> Path:
    return CONFIG_TOML.parent / "groups" / f"{name}.toml"


def parse_group(text: str, source: str) -> dict:
    """A group's Hermes settings: keys of Hermes's config.yaml, in TOML."""
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{source}: {e}") from e
    try:
        json.dumps(config, allow_nan=False)   # as the policy writes them
    except (TypeError, ValueError):
        raise SettingsError(f"{source}: Hermes's settings take strings, numbers, booleans, lists and tables: "
                            "no dates, nan or inf") from None
    if "webapp" in config:
        raise SettingsError(f"{source}: webapp is thinny's: it gives each instance its address and port")
    return config


def parse(text: str, source: str = "config.toml") -> Settings:
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
    settings = Settings(**values)
    _validate(settings, source)
    return settings


def fill(template: str, values: dict[tuple[str, str], object]) -> str:
    """config.toml text: `template` with `values` ((section, key) -> value) set, its comments kept (in their
    column when the value fits)."""
    lines, section = [], ""
    for line in template.splitlines():
        if m := re.match(r"\[(\w+)\]", line):
            section = m[1]
        elif (m := re.match(r"(\w+) = .*?(?:\s+(#.*))?$", line)) and (section, m[1]) in values:
            new = f"{m[1]} = {json.dumps(values[section, m[1]])}"  # JSON strings, numbers and booleans are TOML
            line = f"{new:<{m.start(2) - 1}} {m[2]}" if m[2] else new
        lines.append(line)
    return "\n".join(lines) + "\n"


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

    for model in s.allowed_models:
        if not model or "<" in model:
            fail("set policy.allowed_models to real model ids")
    if s.model_lock and s.model not in s.allowed_models:   # so it is required
        fail("with policy.model_lock, set policy.model to one of policy.allowed_models")
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
    if s.frontend not in FRONTENDS:
        fail("webapp.frontend must be webapp or webui")
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
    if s.tunnel_port and not (1024 <= s.tunnel_port <= 65535) or s.port_range[0] <= s.tunnel_port <= s.port_range[1]:
        fail("access.tunnel_port must be 0 (off) or a port between 1024 and 65535 outside webapp.port_range")
    for network in s.blocked_networks:
        try:
            ipaddress.ip_network(network)
        except ValueError:
            fail(f"network.blocked_networks: {network!r} is not a network")
    if s.telegram_manager_token and not re.fullmatch(r"\d+:[\w-]+", s.telegram_manager_token):
        fail("telegram.manager_token must be the manager bot's token from @BotFather, like 123456:ABC-xyz")
    if s.dashboard_access not in DASHBOARD_ACCESS:
        fail(f"dashboard.access must be one of {', '.join(DASHBOARD_ACCESS)}")
    if s.dashboard_retention_days < 1:
        fail("dashboard.retention_days must be at least 1")
    if not s.dashboard_enabled:   # its defaults take nothing from a server without it
        return
    if s.dashboard_access == "lan" and not s.lan_address:
        fail("access.lan_address is required when dashboard.access is lan")
    low, high = s.port_range
    if not 1024 <= s.dashboard_port <= 65535 or low <= s.dashboard_port <= high or \
            s.dashboard_port in (s.tunnel_port, *MONITORING_PORTS):
        fail("dashboard.port must be a port between 1024 and 65535 outside webapp.port_range, other than "
             f"access.tunnel_port and {', '.join(map(str, MONITORING_PORTS))}")
    if any(low <= port <= high or port == s.tunnel_port for port in MONITORING_PORTS):
        fail(f"with the dashboard, webapp.port_range and access.tunnel_port must leave out "
             f"{', '.join(map(str, MONITORING_PORTS))}: it uses them on 127.0.0.1")
