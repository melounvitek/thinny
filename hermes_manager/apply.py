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
    active = [p for p in reg.current() if p.state == "active"]
    host.install_units(s)
    if not host.managed_scope_current(s):
        log("the managed scope changes: every gateway restarts")
        people.mark_restart([p.name for p in active])  # before the change, so a failure keeps the intent
        host.write_managed_scope(s)
    host.ensure_plugins(s)
    host.fix_permissions()
    host.firewall(s)
    for p in active:
        p.url = people.url_for(s, p)
        people.configure_webapp(s, p)  # remembers the restart when it changes something
    reg.save()
    host.reconcile_access(s, reg)
    people.restart_all([], reg)
    log("applied")
