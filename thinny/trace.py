"""The trace of admins' access to containers: every `incus exec` (`incus shell` too) and `incus console`
that is not thinny's own, from Incus's lifecycle events. It goes to the journal (and so to syslog) and,
for an instance, to its trace directory, which its container sees read-only. A trace, not a guard: root
on the host can stop it, or reach a container around Incus."""

from __future__ import annotations

import datetime as dt
import json
import shlex
import subprocess
import sys

from .system import INTERNAL, STATE_DIR

DIR = STATE_DIR / "trace"   # an instance's admin-access.log in DIR/<name>
IN_CONTAINER = "/var/log/thinny"   # DIR/<name>, as the instance's container sees it
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


def run() -> int:
    """Trace until the monitor ends (incusd stopped or restarted); systemd then starts the trace again."""
    with subprocess.Popen(MONITOR, stdout=subprocess.PIPE, encoding="utf-8", errors="replace") as monitor:
        for raw in monitor.stdout:
            if not raw.strip():   # every event ends with an empty line
                continue
            event = json.loads(raw)
            text = line(event)
            if text is None:
                continue
            print(text, flush=True)
            path = DIR / event["metadata"]["name"]
            if path.is_dir():   # an instance's
                try:
                    with (path / "admin-access.log").open("a", encoding="utf-8") as f:
                        f.write(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {text}\n")
                except OSError as e:
                    print(f"{path}: {e}", file=sys.stderr, flush=True)
    return 1
