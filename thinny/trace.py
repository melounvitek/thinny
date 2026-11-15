"""The trace of admins' access to containers: every `incus exec` (`incus shell` too) and `incus console`,
`incus file`, snapshots and exports, from Incus's lifecycle events and its log of API requests; thinny's own
commands once per run. It goes to the journal (and so to syslog) and,
for an instance, to its trace directory, which its container sees read-only, and to its Google Sheet if
it has one. A trace, not a guard: root on the host can stop it, or reach a container around Incus."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from .host import THINNY
from .registry import Registry, RegistryError
from .system import STATE_DIR, CommandError, write_file

DIR = STATE_DIR / "trace"   # an instance's admin-access.log in DIR/<name>
IN_CONTAINER = "/var/log/thinny"   # DIR/<name>, as the instance's container sees it
UNSENT = STATE_DIR / "trace-unsent"   # each row for an instance's sheet as UNSENT/<name>/<number>.json until sent
RETRY = 300   # seconds between tries to send the rows a sheet did not take
# A sheet's Apps Script as a web app. Nothing else: the server would post the trace wherever an URL points.
SHEET_URL = re.compile(r"https://script\.google\.com/macros/s/[\w-]+/exec")
MONITOR = ["incus", "monitor", "--project", "default", "--type", "lifecycle", "--type", "logging", "--format", "json"]
PROC = Path("/proc")
PYTHON = "/usr/bin/python3"   # thinny's, bin/thinny says
MARK = re.compile(r"THINNY=(\d+)")   # system.INTERNAL: thinny's process
# An event's source or a request's URL: the instance and what of it ("", "sftp", "snapshots/snap0", ...).
INSTANCE = re.compile(r"/1\.0/instances/([A-Za-z0-9][A-Za-z0-9-]*)/?([^?]*)(\?.*)?")
# The other lifecycle events of access, as the admin's command. The file API's are other clients' than `incus`.
COMMANDS = {"instance-console": "incus console {name}",
            "instance-console-retrieved": "incus console {name} --show-log",
            "instance-file-retrieved": "incus file pull {name}/{path}",
            "instance-file-pushed": "incus file push {name}/{path}",
            "instance-file-deleted": "incus file delete {name}/{path}",
            "instance-snapshot-created": "incus snapshot create {name} {item}",
            "instance-backup-created": "incus export {name}"}


def thinny_run(pid: str) -> str | None:
    """`thinny <arguments>` the process runs; None when it runs no thinny."""
    try:
        python = os.path.samefile(PROC / pid / "exe", PYTHON)
        argv = (PROC / pid / "cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]
    except OSError:   # it ended
        return None
    if not python or argv[1:2] != [THINNY]:   # Python runs thinny as its script
        return None
    return shlex.join(["thinny", *argv[2:]])


def entry(event: dict, noted: set[tuple[str, str]]) -> tuple[str, str] | None:
    """The instance and the admin's command, or the run of thinny if not noted for the instance yet; None for
    other events. `noted`: the instances and processes of the runs of thinny noted."""
    meta = event["metadata"]
    if event["type"] == "logging":
        # `incus file` goes through SFTP, which sends no lifecycle event: only its request is logged.
        url = (meta.get("context") or {}).get("url", "")
        found = meta["message"] == "Handling API request" and INSTANCE.fullmatch(url)
        if not found or found[2] != "sftp":
            return None
        name, text = found[1], f"incus file (pull, push, edit or mount) {found[1]}"
    else:
        found = INSTANCE.fullmatch(meta["source"])
        action, context = meta["action"], meta.get("context") or {}
        if not found or (action != "instance-exec" and action not in COMMANDS):
            return None
        name = found[1]
        if action == "instance-exec":
            command = context["command"]
            mark = MARK.fullmatch(command[1]) if len(command) > 1 and command[0] == "env" else None
            run = thinny_run(mark[1]) if mark else None   # only a running thinny's mark counts: anyone can put one in
            if run:
                if (name, mark[1]) in noted:
                    return None
                noted.add((name, mark[1]))
            text = f"{run} ran its commands in {name}" if run else f"incus exec {name} -- {shlex.join(command)}"
        else:
            text = COMMANDS[action].format(name=name, path=str(context.get("path", "")).lstrip("/"),
                                           item=found[2].rpartition("/")[2])
    # One line, with nothing a terminal would act on.
    return name, "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


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


def sheet_of(name: str) -> str | None:
    """The URL of the instance's sheet, "" without one; None while the registry cannot be read."""
    try:
        inst = Registry.load().instances.get(name)
    except Exception as e:  # noqa: BLE001 - its rows wait until it can be read again
        print(f"cannot read the registry: {e}", file=sys.stderr, flush=True)
        return None
    return inst.trace_sheet if inst else ""


def send_waiting(name: str) -> None:
    """The instance's rows, oldest first, each to the sheet the registry names once it is found, until one
    fails (`instance trace-sheet` may change it meanwhile). Without a sheet, a row goes."""
    rows = sorted((UNSENT / name).glob("*.json"))
    for i, path in enumerate(rows):
        url = sheet_of(name)
        if url is None:
            return
        if url:
            try:
                send(url, json.loads(path.read_text(encoding="utf-8")))
            except CommandError as e:
                print(f"{name}: {e}; rows waiting for its sheet: {len(rows) - i}", file=sys.stderr, flush=True)
                return
        path.unlink(missing_ok=True)


def sender(wake: threading.Event, stop: threading.Event) -> None:
    """Send what waits for the sheets at once, then whenever the trace adds a row, every RETRY seconds, and a
    last time once stopped. On its own thread: a slow or failing sheet must not hold up the trace."""
    while True:
        last = stop.is_set()   # before the round: one that began earlier may miss the latest rows
        for folder in sorted(UNSENT.glob("*")):
            try:
                send_waiting(folder.name)
            except Exception as e:  # noqa: BLE001 - the next round tries again
                print(f"{folder.name}: {e}", file=sys.stderr, flush=True)
        if last:
            return
        wake.wait(RETRY)
        wake.clear()


def connect_sheet(reg: Registry, name: str, url: str) -> None:
    """The Google Sheet that gets the instance's access trace too, by its Apps Script's URL; "off": none. A
    sheet is told when it starts and when it stops getting it."""
    inst = reg.get(name)
    if inst.state != "active":
        raise RegistryError(f"{name} is {inst.state}")
    if url != "off" and not SHEET_URL.fullmatch(url):
        raise CommandError(f"{url} is not the URL of a sheet's script: https://script.google.com/macros/s/<id>/exec "
                           "(see README, Access trace)")

    def row(entry: str) -> dict:
        return {"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "instance": name, "entry": entry}
    if url != "off":
        send(url, row("thinny sends this instance's access trace here from now on"))
    if inst.trace_sheet and inst.trace_sheet != url:
        try:
            send(inst.trace_sheet, row("thinny stopped sending this instance's access trace here"))
        except CommandError as e:
            print(f"Could not tell {name}'s previous sheet: {e}", file=sys.stderr)
    inst.trace_sheet = "" if url == "off" else url
    reg.save()


def run() -> int:
    """Trace until the monitor ends (incusd stopped or restarted); systemd then starts the trace again."""
    wake, stop = threading.Event(), threading.Event()
    sending = threading.Thread(target=sender, args=(wake, stop), daemon=True)
    sending.start()
    noted = set()
    with subprocess.Popen(MONITOR, stdout=subprocess.PIPE, encoding="utf-8", errors="replace") as monitor:
        for raw in monitor.stdout:
            if not raw.strip():   # every event ends with an empty line
                continue
            found = entry(json.loads(raw), noted)
            if found is None:
                continue
            name, text = found
            print(text, flush=True)
            path = DIR / name
            if path.is_dir():   # an instance's
                now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                try:
                    with (path / "admin-access.log").open("a", encoding="utf-8") as f:
                        f.write(f"{now} {text}\n")
                except OSError as e:
                    print(f"{path}: {e}", file=sys.stderr, flush=True)
                try:   # for its sheet, if it has one: the sender knows. Numbered, as the clock may step back.
                    rows = (UNSENT / name).glob("*.json")
                    number = max((int(row.stem) for row in rows if row.stem.isdigit()), default=0) + 1
                    write_file(UNSENT / name / f"{number:012}.json",
                               json.dumps({"time": now, "instance": name, "entry": text}), 0o600)
                    wake.set()
                except OSError as e:
                    print(f"{name}: its sheet misses this entry: {e}", file=sys.stderr, flush=True)
    stop.set()
    wake.set()
    sending.join(5)   # what is left goes now, or when the trace starts again
    return 1
