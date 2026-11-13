"""Hermes update (thinny update, nightly with --scheduled): every person's Hermes, one container
at a time, to exactly the same release."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import host, people
from .registry import Registry
from .settings import Settings
from .system import STATE_DIR, log, write_file

STATUS_FILE = STATE_DIR / "update-status.json"
# "<tag> <commit> [<person being updated>]" while an update runs; a later run finishes the job.
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
        if result == "ok" and REBOOT_REQUIRED.exists():
            detail += "; the OS needs a reboot"
    except Exception as e:  # noqa: BLE001 - any failure must end as FAILED
        result, detail = "FAILED", str(e) or type(e).__name__
    log(f"{result}: {detail}")
    if not dry_run:
        status = {"time": dt.datetime.now().isoformat(timespec="seconds"), "result": result, "detail": detail}
        write_file(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=1) + "\n")
    return 1 if result == "FAILED" else 0


def due(s: Settings, *, dry_run: bool, now: bool) -> tuple[str, str, str]:
    """(result, detail, tag) for the latest stable release: up-to-date, waiting, available or due."""
    tag, published = host.latest_release()
    running = (host.release() or ("nothing",))[0]
    age = (dt.datetime.now(dt.UTC) - published).total_seconds() / 3600
    log(f"running: {running}; latest stable: {tag}, out {age:.0f} h")
    if running == "nothing":
        return "up-to-date", "nobody has Hermes yet", tag
    if tag == running:
        return "up-to-date", tag, tag
    if age < s.min_release_age_hours and not now:
        return "waiting", f"{tag} is {age:.0f} h old, waiting for {s.min_release_age_hours:.0f} h", tag
    if dry_run or not (s.auto_update or now):  # `now` is also the manual update when automatic is off
        return "available", tag + ("" if s.auto_update else "; automatic updates are off"), tag
    return "due", "", tag


def rollout(s: Settings, reg: Registry, tag: str, commit: str, current: str = "") -> None:
    """Every active person to `commit`, one at a time, each followed by a checked restart. Stops
    at the first failure. A repeat starts with the person it stopped at (their checkout may be at
    the commit while the rest of their update is not), then skips the people already done. New
    people get the release once everyone runs it."""
    active = [p for p in reg.current() if p.state == "active"]
    for p in sorted(active, key=lambda p: p.name != current):  # the person it stopped at first
        if p.name != current and people.installed_commit(p.name) == commit:
            continue
        log(f"{p.name}: Hermes {tag}")
        write_file(UNFINISHED, f"{tag} {commit} {p.name}\n", 0o600)
        people.mark_restart([p.name])
        people.ensure_packages(p.name)  # a new Hermes may need packages the list gained since
        people.install_hermes(s, p.name, commit, enable=False)
        people.restart_all(s, [p], reg)  # with the restarts an interrupted run still owes
    people.restart_all(s, [], reg)
    host.write_release(tag, commit)
    UNFINISHED.unlink(missing_ok=True)
