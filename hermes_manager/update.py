"""Nightly Hermes update (hermes-manager update): the shared install, then every person's
environment, gateway and sandboxes."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import tarfile
from pathlib import Path

from . import host, people
from .registry import Registry
from .settings import Settings
from .system import STATE_DIR, CommandError, as_user, hermes_root, log, write_file

STATUS_FILE = STATE_DIR / "update-status.json"
# "<phase> <tag>" while an update runs: "code" until the shared install is at the tag, then
# "rollout" until every gateway runs it. A later run finishes the job.
UNFINISHED = STATE_DIR / "update-unfinished"
BACKUP_DIR = Path("/var/backups/hermes-manager")
BACKUPS_KEPT = 14
REBOOT_REQUIRED = Path("/run/reboot-required")


def run(s: Settings, reg: Registry, *, check: bool = False, now: bool = False) -> int:
    """Update when the latest stable release is old enough. `check` only reports: no changes and
    no status file, so the last nightly result stays visible."""
    log(f"=== hermes-manager update{' --check' if check else ''}{' --now' if now else ''}")
    try:
        phase, _, target = UNFINISHED.read_text().strip().partition(" ") if UNFINISHED.exists() else ("", "", "")
        if phase == "rollout" and not check:  # the install is at the tag already: no release lookup needed
            log(f"finishing the interrupted update to {target}")
            rollout(s, reg)
            result, detail = "ok", f"finished the interrupted update to {target}"
        else:
            result, detail, tag = due(s, check=check, now=now, resume=phase == "code")
            if result == "due":
                update(s, reg, tag)
                result, detail = "ok", f"updated to {tag}"
        if result == "ok" and REBOOT_REQUIRED.exists():
            detail += "; the OS needs a reboot"
    except Exception as e:  # noqa: BLE001 - any failure must end as FAILED
        result, detail = "FAILED", str(e) or type(e).__name__
    finally:
        if not check:
            # root's Hermes runs tighten /opt/hermes, which breaks every person's `hermes`
            with contextlib.suppress(Exception):
                host.fix_permissions()
    log(f"{result}: {detail}")
    if not check:
        status = {"time": dt.datetime.now().isoformat(timespec="seconds"), "result": result, "detail": detail}
        write_file(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=1) + "\n")
    return 1 if result == "FAILED" else 0


def due(s: Settings, *, check: bool, now: bool, resume: bool) -> tuple[str, str, str]:
    """(result, detail, tag) for the latest stable release: up-to-date, waiting, available or due.
    `resume`: a code update was cut off, so it is due whatever its age or auto_update."""
    tag, published = host.latest_release()
    installed = host.installed_tags()
    age = (dt.datetime.now(dt.UTC) - published).total_seconds() / 3600
    log(f"installed: {', '.join(sorted(installed)) or 'untagged'}; latest stable: {tag}, out {age:.0f} h")
    if resume and not check:
        return "due", "", tag
    if tag in installed:
        return "up-to-date", tag, tag
    if age < s.min_release_age_hours and not now:
        return "waiting", f"{tag} is {age:.0f} h old, waiting for {s.min_release_age_hours:.0f} h", tag
    if check or not (s.auto_update or now):  # --now is also the manual update when automatic is off
        return "available", tag + ("" if s.auto_update else "; automatic updates are off"), tag
    return "due", "", tag


def update(s: Settings, reg: Registry, tag: str) -> None:
    log(f"backup: {backup(s)}")
    write_file(UNFINISHED, f"code {tag}\n", 0o600)
    if tag not in host.installed_tags():  # an interrupted run may have got this far
        log(f"updating Hermes to {tag}")
        res = hermes_root(["update", "--yes", "--no-gateway-restart"], timeout=3600)
        for line in (res.stdout + res.stderr).strip().splitlines()[-8:]:
            log(f"  | {line}")
        if tag not in host.installed_tags():
            raise CommandError(f"hermes update finished but the install is not at {tag}")
    write_file(UNFINISHED, f"rollout {tag}\n", 0o600)
    rollout(s, reg)


def rollout(s: Settings, reg: Registry) -> None:
    """Everything after the code changed: permissions, plugins, Webapp, people's environments,
    gateways, sandboxes. Safe to repeat."""
    log("restoring permissions and local plugins")
    host.fix_permissions()
    host.ensure_plugins(s)
    host.build_webapp(force=True)
    host.fix_permissions()

    active = [p for p in reg.current() if p.state == "active"]
    for p in active:
        # The automatic resync at launch is refused for the root-owned checkout, and `pm repair` only
        # rebuilds the old dependency set; `pm install --extra all` syncs to the new lock in the
        # person's own home (and skips the root-owned tool store).
        log(f"{p.name}: Hermes environment (hermes pm install --extra all)")
        people.hermes(p.name, ["pm", "install", "--extra", "all"], timeout=1800)
    people.restart_all(active, reg)
    for p in active:
        refresh_sandboxes(s, p.name)
    UNFINISHED.unlink(missing_ok=True)


def backup(s: Settings) -> Path:
    """/etc/hermes and the local plugins; the newest BACKUPS_KEPT stay."""
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = BACKUP_DIR / f"{dt.datetime.now():%Y-%m-%d-%H%M}.tgz"
    with tarfile.open(path, "w:gz") as tar:
        tar.add(host.MANAGED, arcname="etc-hermes")
        for name in [n for n in ("model-guard", s.language_pack) if n]:
            tar.add(host.CHECKOUT / "plugins" / name, arcname=f"plugins/{name}")
    for old in sorted(BACKUP_DIR.glob("*.tgz"))[:-BACKUPS_KEPT]:
        old.unlink()
    return path


def refresh_sandboxes(s: Settings, name: str) -> None:
    """Pull the sandbox image (Hermes pulls only a missing one, so a floating tag never moves).
    A new image replaces the person's sandboxes; otherwise running ones must still reach the
    internet. Hermes recreates a removed sandbox on next use."""
    def podman(*args: str) -> str:
        return as_user(name, ["podman", *args], check=False, timeout=120).stdout.strip()

    def sandboxes(*flags: str) -> list[str]:
        return [n for n in podman("ps", *flags, "--format", "{{.Names}}").split() if n.startswith("hermes-")]

    before = podman("image", "inspect", "--format", "{{.Id}}", s.sandbox_image)
    log(f"{name}: pulling the sandbox image")
    try:
        as_user(name, ["podman", "pull", s.sandbox_image], timeout=1800)
    except CommandError as e:
        log(f"  ! {name}: sandbox image pull failed: {e}")
    if podman("image", "inspect", "--format", "{{.Id}}", s.sandbox_image) != before:
        remove = sandboxes("-a")
        log(f"{name}: new sandbox image, removing {len(remove)} sandbox(es)")
    else:
        remove = []
        for c in sandboxes():
            code = podman("exec", c, "curl", "-s", "-o", "/dev/null", "-m", "8", "-w", "%{http_code}",
                          "https://example.com/")
            if code.startswith(("2", "3")):
                log(f"  sandbox {name}/{c}: network ok")
            else:
                log(f"  ! sandbox {name}/{c}: no network (HTTP {code or '-'}), removing it")
                remove.append(c)
    for c in remove:
        podman("rm", "-f", c)
        # A container whose conmon died lands in "Exited (-1)" after the first rm; the second removes it.
        podman("rm", "-f", "--ignore", c)
    podman("image", "prune", "-f")  # the replaced image (~4 GB per person)
