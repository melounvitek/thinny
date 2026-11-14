"""thinny uninstall: every instance's container and everything thinny set up, removed. Each step skips
what is already gone, so a rerun after a failure continues."""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import sys
from pathlib import Path

from . import host, instances, render, settings, system, update
from .registry import Registry
from .system import CommandError, log

SUDOERS = Path("/etc/sudoers.d/thinny-checks")   # README's rule for status and doctor
COMMAND = Path(host.THINNY)
LIB = Path("/usr/local/lib/thinny")     # where install.sh put thinny; host.LIB is wherever this code runs from
CLONE = Path("/usr/local/src/thinny")   # bin/install's clone


def run() -> None:
    """Once the admin types the hostname on a terminal, remove every instance's container (whatever its
    state), then thinny's host setup, then thinny itself. A failed step stops it with the registry kept.
    Packages stay: other software may use them."""
    reg = Registry.load()
    if not sys.stdin.isatty():
        raise CommandError("no terminal to confirm on; nothing removed")
    print("This deletes every instance, with all its data:")
    print("\n".join(f"  {inst.name} ({inst.state})" for inst in reg.instances.values()) or "  (none)")
    print("It also removes what thinny set up: the Incus profile and storage pool (unless something else\n"
          "uses the pool), the firewall table, the nightly update, the Webapps' Caddy and Tailscale sites,\n"
          f"{settings.SITE_TOML.parent}, {host.MANAGED}, {system.STATE_DIR} and thinny itself.")
    hostname = socket.gethostname()
    try:
        answer = input(f"Type this server's hostname ({hostname}) to remove it all: ")
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer.strip() != hostname:
        raise CommandError("nothing removed")

    if (host.SYSTEMD / "thinny-update.timer").exists():
        system.run(["systemctl", "disable", "--now", "thinny-update.timer"])
        system.run(["systemctl", "stop", "thinny-update.service"])
    for inst in reg.instances.values():
        if instances.container_state(inst.name) is not None:
            log(f"deleting {inst.name}'s container")
            system.run(["incus", "delete", "--force", inst.name])
    if host.tailscale_status() is not None:
        out = system.run(["tailscale", "serve", "status", "--json"]).stdout
        try:
            served = {int(port) for port in json.loads(out).get("TCP") or {}}
        except ValueError as e:
            raise CommandError(f"tailscale serve status --json: {e}") from None
        for port in sorted(served & {inst.port for inst in reg.instances.values()}):
            system.run(["tailscale", "serve", f"--https={port}", "off"])
    if host.CADDYFILE.exists() and render.HEADER in host.CADDYFILE.read_text():
        system.run(["systemctl", "disable", "--now", "caddy"])
        host.CADDYFILE.unlink()
    profiles = system.run(["incus", "profile", "list", "--format", "csv", "--columns", "n"]).stdout.splitlines()
    if render.PROFILE in profiles:
        system.run(["incus", "profile", "delete", render.PROFILE])
    # The pool may be older than thinny (init_incus keeps one), so it goes only if nothing else uses it. The
    # images Incus cached for the containers keep it busy; Incus downloads them again when needed.
    pool_kept = False
    pools = system.run(["incus", "storage", "list", "--format", "csv", "--columns", "n"]).stdout.splitlines()
    if render.POOL in pools:
        for image in json.loads(system.run(["incus", "image", "list", "--format", "json"]).stdout):
            if image["cached"]:
                system.run(["incus", "image", "delete", image["fingerprint"]], check=False)
        pool_kept = system.run(["incus", "storage", "delete", render.POOL], check=False).returncode != 0
    for unit in ("thinny-update.service", "thinny-update.timer", "incus.service.d/thinny.conf"):
        (host.SYSTEMD / unit).unlink(missing_ok=True)
    with contextlib.suppress(OSError):  # gone, or with the admin's own drop-ins
        (host.SYSTEMD / "incus.service.d").rmdir()
    system.run(["systemctl", "daemon-reload"])
    if "table inet thinny" in system.run(["nft", "list", "tables"]).stdout.splitlines():
        system.run(["nft", "delete", "table", "inet", "thinny"])  # incus.service no longer loads it
    host.SYSCTL_FILE.unlink(missing_ok=True)  # the running values stay: Docker or Tailscale may need forwarding
    SUDOERS.unlink(missing_ok=True)
    for path in (host.MANAGED, settings.SITE_TOML.parent, system.STATE_DIR):
        if path.exists():
            shutil.rmtree(path)
    system.LOG_FILE.unlink(missing_ok=True)
    source = LIB / "source"
    clone = source.read_text().strip() if source.exists() else ""
    if clone == str(CLONE) and CLONE.exists():
        shutil.rmtree(CLONE)
    # Last: until here a failure leaves a thinny to run again. Python has its modules loaded.
    COMMAND.unlink(missing_ok=True)
    if LIB.exists():
        shutil.rmtree(LIB)
    update.SELF_UPDATE_LOCK.unlink(missing_ok=True)
    system.LOCK_FILE.unlink(missing_ok=True)

    print("thinny is removed.\n"
          "  The packages stay (Incus, Caddy, btrfs-progs, nftables, curl, Git, Python 3);\n"
          "  apt purge removes those nothing else uses.\n"
          "  IP forwarding and user namespaces stay on until the next reboot.")
    if pool_kept:
        print(f"  The Incus storage pool {render.POOL} stays (up to 80% of the disk): something else uses it.\n"
              f"  See: incus storage volume list {render.POOL}")
    if clone and clone != str(CLONE):
        print(f"  The clone {clone} stays.")
