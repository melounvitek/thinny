"""The trace of admins' access to containers: every `incus exec` (`incus shell` too) and `incus console`
that is not thinny's own, from Incus's lifecycle events. It goes to the journal (and so to syslog) and,
for an instance, to its trace directory, which its container sees read-only, and to its Google Sheet if
it has one. A trace, not a guard: root on the host can stop it, or reach a container around Incus."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import shlex
import subprocess
import sys
import urllib.request

from .registry import Registry, RegistryError
from .system import INTERNAL, STATE_DIR, CommandError, write_file

DIR = STATE_DIR / "trace"   # an instance's admin-access.log in DIR/<name>
IN_CONTAINER = "/var/log/thinny"   # DIR/<name>, as the instance's container sees it
UNSENT = STATE_DIR / "trace-unsent"   # rows an instance's sheet has not taken yet, as JSON lines in UNSENT/<name>
MONITOR = ["incus", "monitor", "--project", "default", "--type", "lifecycle", "--format", "json"]


def line(event: dict) -> str | None:
    """The admin's command; None for thinny's own commands and other events."""
    meta = event["metadata"]
    if meta["action"] == "instance-console":
        return f"incus console {meta['name']}"
    if meta["action"] != "instance-exec" or meta["context"]["command"][:len(INTERNAL)] == INTERNAL:
        return None
    text = f"incus exec {meta['name']} -- {shlex.join(meta['context']['command'])}"
    # One line, with nothing a terminal would act on.
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def send(url: str, row: dict) -> None:
    """A row to the Apps Script of a sheet (README), which answers "ok" once it has added it. Google answers
    the POST with a redirect to the script's answer."""
    request = urllib.request.Request(url, data=json.dumps(row).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = response.read(200).decode(errors="replace")
    except (OSError, http.client.HTTPException) as e:
        raise CommandError(f"the sheet did not answer: {e}") from None
    if answer != "ok":
        raise CommandError(f"the sheet's script answered {answer[:60]!r}, not ok (is it open to anyone?)")


def sheet_of(name: str) -> str:
    try:
        inst = Registry.load().instances.get(name)
    except (OSError, ValueError, TypeError, RegistryError) as e:
        print(f"cannot read the registry: {e}", file=sys.stderr, flush=True)
        return ""
    return inst.trace_sheet if inst else ""


def deliver(name: str, url: str, row: dict | None = None) -> None:
    """Add `row` to the rows that wait for the instance's sheet, then send them, oldest first, until one fails."""
    path = UNSENT / name
    if row is not None:
        UNSENT.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    rows = path.read_text(encoding="utf-8").splitlines()
    sent = 0
    for raw in rows:
        try:
            send(url, json.loads(raw))
        except CommandError as e:
            print(f"{name}: {e}; rows waiting for its sheet: {len(rows) - sent}", file=sys.stderr, flush=True)
            break
        sent += 1
    if sent == len(rows):
        path.unlink()
    elif sent:
        write_file(path, "".join(raw + "\n" for raw in rows[sent:]), 0o600)


def run() -> int:
    """Trace until the monitor ends (incusd stopped or restarted); systemd then starts the trace again."""
    for path in sorted(UNSENT.glob("*")):   # what an outage kept from a sheet
        if url := sheet_of(path.name):
            deliver(path.name, url)
    with subprocess.Popen(MONITOR, stdout=subprocess.PIPE, encoding="utf-8", errors="replace") as monitor:
        for raw in monitor.stdout:
            if not raw.strip():   # every event ends with an empty line
                continue
            event = json.loads(raw)
            text = line(event)
            if text is None:
                continue
            print(text, flush=True)
            name = event["metadata"]["name"]
            path = DIR / name
            if path.is_dir():   # an instance's
                time = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                try:
                    with (path / "admin-access.log").open("a", encoding="utf-8") as f:
                        f.write(f"{time} {text}\n")
                except OSError as e:
                    print(f"{path}: {e}", file=sys.stderr, flush=True)
                if url := sheet_of(name):
                    deliver(name, url, {"time": time, "instance": name, "entry": text})
    return 1
