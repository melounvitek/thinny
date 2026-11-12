"""`install` (the shared Hermes install) and `apply` (bring everything in line with site.toml)."""

from __future__ import annotations

from . import host, people
from .registry import Registry
from .settings import Settings
from .system import log


def install(s: Settings) -> None:
    host.install_hermes()
    host.install_files()
    host.ensure_plugins(s)
    host.fix_permissions()
    host.build_webapp()
    host.fix_permissions()
    apply(s)
    print("Hermes is installed. Add people with: hermes-manager add-person <name>")


def apply(s: Settings) -> None:
    reg = Registry.load()
    host.install_units(s)
    managed_changed = host.write_managed_scope(s)
    host.ensure_plugins(s)
    host.fix_permissions()
    host.firewall(s, reg)

    active = [p for p in reg.current() if p.state == "active"]
    to_restart = []
    for p in active:
        p.url = people.url_for(s, p)
        if people.configure_webapp(s, p):
            to_restart.append(p)
    reg.save()
    host.reconcile_access(s, reg)

    if managed_changed:
        log("the managed scope changed: restarting every gateway")
        to_restart = active
    people.restart_all(to_restart)
    log("applied")
