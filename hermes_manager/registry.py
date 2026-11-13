"""People registry (/var/lib/hermes-manager/people.json), the source of truth for who exists.

People are never deleted: a removed person stays as `disabled`, so their container, its
address and their Webapp port are never handed to someone else.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

REGISTRY = Path("/var/lib/hermes-manager/people.json")
STATES = ("pending", "active", "disabled")
SUBNET = ipaddress.ip_network("10.78.0.0/24")   # container addresses
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]")


class RegistryError(Exception):
    pass


@dataclass
class Person:
    name: str
    port: int
    access: str
    url: str = ""
    address: str = ""
    state: str = "pending"


def validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise RegistryError(
            f"invalid name {name!r}: 2-32 characters, lowercase letters, digits and '-', "
            "starting with a letter")


class Registry:
    def __init__(self, people: dict[str, Person] | None = None):
        self.people = people or {}

    @classmethod
    def load(cls, path: Path = REGISTRY) -> Registry:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        return cls({name: Person(name=name, **fields) for name, fields in data["people"].items()})

    def save(self, path: Path = REGISTRY) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = {"people": {p.name: {k: v for k, v in asdict(p).items() if k != "name"}
                           for p in sorted(self.people.values(), key=lambda p: p.port)}}
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)

    def get(self, name: str) -> Person:
        try:
            return self.people[name]
        except KeyError:
            raise RegistryError(f"no person named {name!r} (see hermes-manager list)") from None

    def add(self, name: str, access: str, port_range: tuple[int, int]) -> Person:
        validate_name(name)
        if name in self.people:
            raise RegistryError(f"{name!r} already exists")
        person = Person(name=name, port=self.next_port(port_range), access=access, address=self.next_address())
        self.people[name] = person
        return person

    def next_port(self, port_range: tuple[int, int]) -> int:
        used = {p.port for p in self.people.values()}
        for port in range(port_range[0], port_range[1] + 1):
            if port not in used:
                return port
        raise RegistryError(f"no free Webapp port left in {port_range[0]}-{port_range[1]}")

    def next_address(self) -> str:
        used = {p.address for p in self.people.values()}
        for address in map(str, SUBNET.hosts()):
            if address not in used:
                return address
        raise RegistryError(f"no free container address left in {SUBNET}")

    def current(self) -> list[Person]:
        """People who are not disabled, in port order."""
        return sorted((p for p in self.people.values() if p.state != "disabled"), key=lambda p: p.port)
