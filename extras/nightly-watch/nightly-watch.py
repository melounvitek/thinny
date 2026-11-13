#!/usr/bin/env python3
"""Morning check of nightly jobs (run by cron on another machine).

Usage: nightly-watch.py <config.json>

Reads each job's status file over SSH. Prints nothing when all jobs are fine; prints a short
alert otherwise (cron mails it). A result older than max_age_hours also alerts: it means the job
did not run at all.
"""
import datetime as dt
import json
import posixpath
import shlex
import subprocess
import sys

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]

# Built-in job kinds; a job in the config can override any of these keys.
KINDS = {
    "hermes-update": {
        "file": "/var/lib/thinny/update-status.json",
        "healthy": ["ok", "up-to-date", "waiting", "available"],
        "hint": "ssh {host} journalctl -u thinny-update -n 40",
    },
    "netcup-snapshot": {  # "file" is required in the job: <data dir>/status.json
        "healthy": ["ok"],
        "hint": "ssh {host} tail -20 {dir}/snapshot.log",
    },
}


def parse_time(value):
    # A time without an offset is taken as this machine's local time.
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()


def check(job, max_age_h):
    try:
        out = subprocess.run(SSH + [job["host"], "cat " + shlex.quote(job["file"])],
                             capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return f"could not reach {job['host']} (timeout)"
    if out.returncode != 0:
        return f"could not read the status from {job['host']}: {(out.stderr or out.stdout).strip()[:200]}"
    try:
        d = json.loads(out.stdout)
        when = parse_time(d["time"])
    except (ValueError, KeyError, TypeError) as e:
        return f"status unreadable ({e}): {out.stdout.strip()[:200]}"
    age_h = (dt.datetime.now().astimezone() - when).total_seconds() / 3600
    stamp = when.strftime("%Y-%m-%d %H:%M")
    if d.get("result") not in job["healthy"]:
        return f"{d.get('result')} ({stamp}): {d.get('detail')}"
    if age_h > max_age_h:
        return f"did not run: the last result is from {stamp} ({age_h:.0f} h ago), its timer or container is probably not running"
    return None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    max_age_h = cfg.get("max_age_hours", 26)
    problems = []
    for job in cfg["jobs"]:
        job = {**KINDS[job["kind"]], **job}
        if msg := check(job, max_age_h):
            hint = job["hint"].format(host=job["host"], dir=posixpath.dirname(job["file"]))
            problems.append(f"- {job['name']}: {msg}\n  Details: {hint}")
    if problems:
        print("A nightly job needs attention:\n")
        print("\n".join(problems))


if __name__ == "__main__":
    main()
