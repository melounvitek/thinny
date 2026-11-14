"""Instance registry (/var/lib/thinny/instances.json), the source of truth for which instances exist.

Instances are never deleted: a disabled instance stays, so its container, its
address and its Webapp port are never handed to another instance.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

REGISTRY = Path("/var/lib/thinny/instances.json")
STATES = ("pending", "active", "disabled")
SUBNET = ipaddress.ip_network("10.78.0.0/24")   # container addresses
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]")


class RegistryError(Exception):
    pass


@dataclass
class Instance:
    name: str
    port: int
    access: str
    url: str = ""
    address: str = ""
    state: str = "pending"
    built_ins: str = ""   # render.BUILT_INS its container last got in full
    moved: bool = False   # from another server: a resumed add keeps its users' settings
    lock: list[str] | None = None   # its own model lock, the models it allows ([]: unlocked); None: the site's
    lock_provider: str = ""         # the provider of its own lock


def validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise RegistryError(
            f"invalid name {name!r}: 2-32 characters, lowercase letters, digits and '-', "
            "starting with a letter")


class Registry:
    def __init__(self, instances: dict[str, Instance] | None = None):
        self.instances = instances or {}

    @classmethod
    def load(cls, path: Path = REGISTRY) -> Registry:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        return cls({name: Instance(name=name, **fields) for name, fields in data["instances"].items()})

    def save(self, path: Path = REGISTRY) -> None:
        # Any account may read the update status next to it (extras/nightly-watch), not list the directory.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o711)
        data = {"instances": {inst.name: {k: v for k, v in asdict(inst).items() if k != "name"}
                              for inst in sorted(self.instances.values(), key=lambda inst: inst.port)}}
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)

    def get(self, name: str) -> Instance:
        try:
            return self.instances[name]
        except KeyError:
            raise RegistryError(f"no instance named {name!r} (see thinny instance list)") from None

    def add(self, name: str, access: str, port_range: tuple[int, int]) -> Instance:
        validate_name(name)
        if name in self.instances:
            raise RegistryError(f"{name!r} already exists")
        instance = Instance(name=name, port=self.next_port(port_range), access=access, address=self.next_address())
        self.instances[name] = instance
        return instance

    def next_port(self, port_range: tuple[int, int]) -> int:
        used = {inst.port for inst in self.instances.values()}
        for port in range(port_range[0], port_range[1] + 1):
            if port not in used:
                return port
        raise RegistryError(f"no free Webapp port left in {port_range[0]}-{port_range[1]}")

    def next_address(self) -> str:
        used = {inst.address for inst in self.instances.values()}
        for address in map(str, SUBNET.hosts()):
            if address not in used:
                return address
        raise RegistryError(f"no free container address left in {SUBNET}")

    def current(self) -> list[Instance]:
        """Instances that are not disabled, in port order."""
        return sorted((inst for inst in self.instances.values() if inst.state != "disabled"), key=lambda inst: inst.port)
