"""`setup` (Incus and everything around the instances) and `apply` (bring everything in line with
site.toml)."""

from __future__ import annotations

from . import host, instances
from .registry import Registry
from .settings import Settings
from .system import log


def setup(s: Settings) -> None:
    host.init_incus()
    apply(s)
    if s.tunnel_port:
        print(f"Point your tunnel at http://127.0.0.1:{s.tunnel_port} (see README, Access).")
    print("Ready. Add instances with: thinny instance add <name>")


def apply(s: Settings) -> None:
    reg = Registry.load()
    active = [inst for inst in reg.current() if inst.state == "active"]
    host.firewall(s)  # before anything could start a container
    host.install_units(s)
    host.ensure_profile(s)
    if not host.managed_scope_current(s):
        log("the managed scope changes: every gateway restarts")
        instances.mark_restart([inst.name for inst in active])  # before the change, so a failure keeps the intent
        if s.model_lock:
            # Before the lock pins them: Hermes drops pinned keys from an instance's config whenever it
            # saves it, and writes a `model:` section left empty as invalid YAML (v0.23.1). Pending
            # instances too: a resumed add would start its gateway under the lock.
            for inst in reg.current():
                for key in instances.starting_model(s):
                    instances.hermes(inst.name, ["config", "unset", key], check=False)  # 1 when not set
        host.write_managed_scope(s)
    for inst in active:
        inst.url = instances.url_for(s, inst)
        instances.configure_webapp(s, inst)  # remembers the restart when it changes something
    reg.save()
    host.reconcile_access(s, reg)
    instances.restart_all(s, [], reg)
    log("applied")
