"""The trace of admins' access to containers: every `incus exec` (`incus shell` too) and `incus console`
that is not thinny's own, from Incus's lifecycle events. It goes to the journal (and so to syslog) and,
for an instance, to its trace directory, which its container sees read-only, and to its Google Sheet if
it has one. A trace, not a guard: root on the host can stop it, or reach a container around Incus."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import re
import shlex
import subprocess
import sys
import threading
import urllib.request

from .registry import Registry, RegistryError
from .system import INTERNAL, STATE_DIR, CommandError, write_file

DIR = STATE_DIR / "trace"   # an instance's admin-access.log in DIR/<name>
IN_CONTAINER = "/var/log/thinny"   # DIR/<name>, as the instance's container sees it
UNSENT = STATE_DIR / "trace-unsent"   # each row for an instance's sheet as UNSENT/<name>/<number>.json until sent
RETRY = 300   # seconds between tries to send the rows a sheet did not take
# A sheet's Apps Script as a web app. Nothing else: the server would post the trace wherever an URL points.
SHEET_URL = re.compile(r"https://script\.google\.com/macros/s/[\w-]+/exec")
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
