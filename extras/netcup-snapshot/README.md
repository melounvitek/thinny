# netcup-snapshot

Takes a nightly online snapshot of a netcup VPS through the netcup SCP REST API and keeps the
newest few. Optional. Before the nightly Hermes update it gives you a way back, because Hermes
databases cannot be downgraded.

Run it **off the server**, from cron or a small container on another machine. The API token
controls the whole netcup account (it can stop servers and revert snapshots), so it should not
live on the server it protects.

## What it does

- Names the snapshot `<prefix>YYYYMMDD` (local date). If that snapshot exists, it takes none.
- Dry-runs first. Online snapshots only: if netcup refuses one, the run fails and the server
  keeps running. The snapshot pauses the VM for about half a minute.
- Keeps the newest `keep` snapshots named `<prefix>YYYYMMDD` and deletes older ones. Other
  snapshots are never touched.
- Writes `snapshot.log` and `status.json` next to the config file.

## Setup

Python 3, standard library only.

1. Put `netcup-snapshot.py` and a copy of `config.example.json` named `config.json` into a
   private directory (`chmod 700 <dir>`) on the other machine. The config will hold the token.
2. `python3 netcup-snapshot.py login <dir>/config.json`: open the printed URL, sign in to the
   netcup SCP and confirm the code. The refresh token is written into the config and your
   servers are listed.
3. Put the server's id into `server_id`.
4. `python3 netcup-snapshot.py check <dir>/config.json` dry-runs an online snapshot; `list`
   shows the server's snapshots.

Instead of the argument, the config path can come from `NETCUP_SNAPSHOT_CONFIG`.

## Config keys

| Key | Default | Meaning |
| --- | --- | --- |
| `server_id` | required | the server's id, as `list` shows it |
| `refresh_token` | written by `login` | SCP offline token; a renewed token is written back |
| `prefix` | `daily-` | name prefix; only `<prefix>YYYYMMDD` snapshots are ever deleted |
| `keep` | `7` | how many of those snapshots to keep |
| `time` | `02:30` | when `daemon` runs, local time |

## Running it

**Cron**, before the Hermes nightly update (03:00 by default):

```
30 2 * * * python3 <dir>/netcup-snapshot.py once <dir>/config.json >/dev/null
```

Everything goes to `snapshot.log`; the [morning watchdog](../nightly-watch/) reports failures.

**Container**: `daemon` sleeps until `time` every day and then runs `once`.

```
docker run -d --name netcup-snapshot --restart unless-stopped \
  --user <uid>:<gid> -e TZ=<time zone> -e NETCUP_SNAPSHOT_CONFIG=/data/config.json \
  -v <script dir>:/app:ro -v <dir>:/data \
  python:3-alpine python -u /app/netcup-snapshot.py daemon
```

Run `login`, `list` or `check` the same way with `docker run --rm`. Mount the directory, not
just the config file: the script replaces the file when it stores a new token.

## Status file

`status.json`, rewritten after every `once`:

```json
{"time": "2026-01-31T02:31:12+01:00", "result": "ok", "detail": "snapshots now: daily-20260125, ..."}
```

`result` is `ok` or `FAILED`; on failure `detail` holds the error. This is the same shape as
hermes-manager's `/var/lib/hermes-manager/update-status.json`.
