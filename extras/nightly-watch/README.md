# nightly-watch

A morning check of the nightly jobs, run by cron on another machine. It reads each job's
status file over SSH. When all is well it prints nothing; otherwise it prints a short alert,
which cron mails to you.

It alerts when a job failed, when the status file cannot be read, or when the last result is
older than `max_age_hours` (the job did not run at all).

Python 3, standard library only, plus `ssh` with a key that works without a prompt (it runs
with `BatchMode=yes`).

## Job kinds

| Kind | Status file | Healthy results |
| --- | --- | --- |
| `hermes-update` | `/var/lib/hermes-manager/update-status.json` | `ok`, `up-to-date`, `waiting`, `available` |
| `netcup-snapshot` | `status.json` next to its config (set `file`) | `ok` |

Both files look like `{"time": "<ISO 8601>", "result": "...", "detail": "..."}`. A time without
an offset is read as the local time of the machine running the check.

## Config

Copy `config.example.json` and fill in the placeholders.

- `max_age_hours`: default `26`.
- `jobs[].name`: shown in the alert.
- `jobs[].kind`: one of the kinds above.
- `jobs[].host`: SSH destination `<user>@<host>`; that user must be able to read the file.
- `jobs[].file`: the status file; required for `netcup-snapshot`.

## Cron

```
MAILTO=<your address>
0 7 * * * python3 <dir>/nightly-watch.py <dir>/config.json
```

An alert looks like this:

```
A nightly job needs attention:

- Nightly snapshot: FAILED (2026-01-31 02:31): online snapshot refused (...); server left running, no snapshot taken
  Details: ssh <user>@<snapshot machine> tail -20 <snapshot dir>/snapshot.log
```
