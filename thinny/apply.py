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
    for inst in reg.current():
        # Without a container, its add stopped early and writes its policy. Also moves containers off the
        # policy all instances once shared.
        if instances.container_state(inst.name) is not None:
            instances.update_policy(s, inst)
            if "model-guard" in instances.devices(inst.name):   # added before READ_ONLY, it had shift
                instances.ensure_device(inst.name, "model-guard", "disk", *instances.READ_ONLY)
    for inst in active:
        inst.url = instances.url_for(s, inst)
        instances.configure_webapp(s, inst)  # remembers the restart when it changes something
    reg.save()
    host.reconcile_access(s, reg)
    instances.restart_all(s, [], reg)
    log("applied")
