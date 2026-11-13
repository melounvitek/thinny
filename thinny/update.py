"""thinny update (nightly with --scheduled): thinny itself from its clone, then every instance's Hermes,
one container at a time, to exactly the same release."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from . import host, instances, render, system
from .registry import Registry
from .settings import Settings
from .system import STATE_DIR, CommandError, as_user, lock, log, write_file

SOURCE = host.LIB / "source"      # the clone install.sh ran from
INSTALLED = host.LIB / "commit"   # its commit, once install.sh set it up
# Not the thinny lock: install.sh runs `thinny setup`, which takes that.
SELF_UPDATE_LOCK = Path("/run/thinny-self-update.lock")
STATUS_FILE = STATE_DIR / "update-status.json"
# "<tag> <commit> [<instance being updated>]" while an update runs; a later run finishes the job.
UNFINISHED = STATE_DIR / "update-unfinished"
REBOOT_REQUIRED = Path("/run/reboot-required")


def run(s: Settings, reg: Registry, *, dry_run: bool = False, now: bool = False) -> int:
    """Update when the latest stable release is old enough, or `now`. `dry_run` only reports: no
    changes and no status file, so the last nightly result stays visible."""
    log(f"=== thinny update{' --dry-run' if dry_run else ''}{'' if now else ' --scheduled'}")
    try:
        unfinished = UNFINISHED.read_text().split() if UNFINISHED.exists() else []
        if unfinished and not dry_run:  # whatever its age, and without asking GitHub
            tag, commit, *current = unfinished
            log(f"finishing the interrupted update to {tag}")
            rollout(s, reg, tag, commit, *current)
            result, detail = "ok", f"finished the interrupted update to {tag}"
        else:
            result, detail, tag = due(s, dry_run=dry_run, now=now)
            if result == "due":
                commit = host.commit_of(tag)
                write_file(UNFINISHED, f"{tag} {commit}\n", 0o600)
                rollout(s, reg, tag, commit)
                result, detail = "ok", f"updated to {tag}"
            elif result in ("up-to-date", "waiting") and \
                    any(inst.built_ins != render.BUILT_INS for inst in reg.current() if inst.state == "active"):
                if dry_run or not (s.auto_update or now):
                    result, detail = "available", "new built-ins" + ("" if s.auto_update else "; automatic updates are off")
                else:  # at the release all instances run
                    tag, commit = host.release()
                    write_file(UNFINISHED, f"{tag} {commit}\n", 0o600)
                    rollout(s, reg, tag, commit)
                    result, detail = "ok", f"new built-ins at {tag}"
        if result == "ok" and REBOOT_REQUIRED.exists():
            detail += "; the OS needs a reboot"
    except Exception as e:  # noqa: BLE001 - any failure must end as FAILED
        result, detail = "FAILED", str(e) or type(e).__name__
    if not dry_run:
        update_claude(reg)
    log(f"{result}: {detail}")
    if not dry_run:
        save_status(result, detail)
    return 1 if result == "FAILED" else 0


def save_status(result: str, detail: str) -> None:
    status = {"time": dt.datetime.now().isoformat(timespec="seconds"), "result": result, "detail": detail}
    write_file(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=1) + "\n")


def update_thinny() -> bool:
    """Pull the clone install.sh ran from and install a new commit with its install.sh, which also runs
    `thinny setup`. True when it did: the rest of the update must run the new code. A commit pulled
    but not installed (a failed install.sh) is installed by the next run."""
    def installed() -> str:
        return INSTALLED.read_text().strip() if INSTALLED.exists() else ""
    with lock(SELF_UPDATE_LOCK):
        try:
            if not SOURCE.exists():
                raise CommandError(f"{SOURCE} is missing; run install.sh from the thinny clone")
            src = SOURCE.read_text().strip()
            # Nobody answers a password prompt at night.
            env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
            system.run(["git", "-C", src, "pull", "--ff-only", "-q"], env=env, timeout=300)
            head = system.run(["git", "-C", src, "rev-parse", "HEAD"]).stdout.strip()
            if installed() == head:
                return False
            log(f"installing thinny {head[:12]}")
            system.run(["sh", f"{src}/install.sh"], timeout=3600)
            if installed() != head:  # the new thinny would install it again, and again
                raise CommandError(f"install.sh did not record {INSTALLED}")
            return True
        except Exception as e:  # noqa: BLE001 - any failure must end as FAILED
            detail = str(e) or type(e).__name__
            log(f"FAILED: thinny: {detail}")
            save_status("FAILED", f"thinny: {detail}")
            raise CommandError(detail) from e


def due(s: Settings, *, dry_run: bool, now: bool) -> tuple[str, str, str]:
    """(result, detail, tag) for the latest stable release: up-to-date, waiting, available or due."""
    tag, published = host.latest_release()
    running = (host.release() or ("nothing",))[0]
    age = (dt.datetime.now(dt.UTC) - published).total_seconds() / 3600
    log(f"running: {running}; latest stable: {tag}, out {age:.0f} h")
    if running == "nothing":
        return "up-to-date", "no instance has Hermes yet", tag
    if tag == running:
        return "up-to-date", tag, tag
    if age < s.min_release_age_hours and not now:
        return "waiting", f"{tag} is {age:.0f} h old, waiting for {s.min_release_age_hours:.0f} h", tag
    if dry_run or not (s.auto_update or now):  # `now` is also the manual update when automatic is off
        return "available", tag + ("" if s.auto_update else "; automatic updates are off"), tag
    return "due", "", tag


def rollout(s: Settings, reg: Registry, tag: str, commit: str, current: str = "") -> None:
    """Every active instance to `commit` and the current built-ins, one at a time, each followed by a
    checked restart. Stops at the first failure. A repeat starts with the instance it stopped at
    (its checkout may be at the commit while the rest of its update is not), then skips the
    instances already done. New instances get the release once all instances run it."""
    active = [inst for inst in reg.current() if inst.state == "active"]
    for inst in sorted(active, key=lambda inst: inst.name != current):  # the instance it stopped at first
        if inst.name != current and inst.built_ins == render.BUILT_INS and instances.installed_commit(inst.name) == commit:
            continue
        log(f"{inst.name}: Hermes {tag}")
        write_file(UNFINISHED, f"{tag} {commit} {inst.name}\n", 0o600)
        instances.mark_restart([inst.name])
        instances.ensure_packages(inst.name)  # a new Hermes may need packages the list gained since
        instances.ensure_account(inst.name)   # and the groups new built-ins need
        # Plugins are turned on only when the built-ins change, so a Hermes update keeps what users turned off.
        instances.install_hermes(s, inst.name, commit, enable=inst.built_ins != render.BUILT_INS)
        instances.restart_all(s, [inst], reg)  # with the restarts an interrupted run still owes
        inst.built_ins = render.BUILT_INS
        reg.save()
    instances.restart_all(s, [], reg)
    host.write_release(tag, commit)
    UNFINISHED.unlink(missing_ok=True)


def update_claude(reg: Registry) -> None:
    """Claude Code for every instance, whatever happened to Hermes: the Claude subscription plugin turns its
    self-update off. A failure is only logged."""
    for inst in reg.current():
        if inst.state == "active":
            try:
                as_user(inst.name, ["claude", "update"], timeout=600)
            except CommandError as e:
                log(f"{inst.name}: Claude Code not updated: {e}")
