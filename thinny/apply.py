"""`setup` (Incus and everything around the instances) and `apply` (bring everything in line with
config.toml)."""

from __future__ import annotations

import os

from . import dashboard, host, instances
from .registry import Registry
from .settings import Settings
from .system import log


def setup(s: Settings) -> None:
    host.init_incus()
    apply(s)
    if s.tunnel_port:
        print(f"Point your tunnel at http://127.0.0.1:{s.tunnel_port} (see README, Access).")
    sudo = "sudo " if os.environ.get("SUDO_USER") else ""
    if host.ufw_lacks_forward():
        print("ufw blocks the containers' traffic, so instances cannot reach the internet (see README, "
              f"Prerequisites). Run: {sudo}{host.UFW_FORWARD}")
    lan = s.access_default == "lan" or any(inst.access == "lan" for inst in Registry.load().current()) or \
        (host.dashboard_access() or (0, ""))[1] == "lan"
    if lan and (missing := host.ufw_lacks(host.ufw_lan_rules(s))):
        print("ufw blocks office PCs, so they cannot open the Webapps or get the root certificate (see README, "
              "Prerequisites). Run:" + "".join(f"\n  {sudo}{rule}" for rule in missing))
    print("Ready. Add instances with: thinny instance add <name>")


def apply(s: Settings) -> None:
    reg = Registry.load()
    active = [inst for inst in reg.current() if inst.state == "active"]
    for inst in reg.current():
        instances.settings_for(s, inst)   # refuses a member of a missing group before anything changes
    host.firewall(s)  # before anything could start a container
    host.install_units(s)
    host.ensure_profile(s)
    dashboard.apply(s, reg)   # before the instances: a question, if any, comes early
    for inst in reg.current():
        # Without a container, its add stopped early and writes its policy. Also moves containers off the
        # policy all instances once shared.
        if instances.container_state(inst.name) is not None:
            instances.update_policy(s, inst)
            if "model-guard" in instances.devices(inst.name):   # added before READ_ONLY, it had shift
                instances.ensure_device(inst.name, "model-guard", "disk", *instances.READ_ONLY)
    for inst in active:
        instances.ensure_trace(inst.name)   # instances added before the trace get it too
        inst.url = instances.url_for(s, inst)
        instances.configure_webapp(s, inst)  # remembers the restart when it changes something
    reg.save()
    host.reconcile_access(s, reg)
    instances.restart_all(s, [], reg)
    log("applied")
