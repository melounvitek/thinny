#!/usr/bin/env python3
"""Nightly online snapshot of a netcup VPS through the netcup SCP REST API.

Run it off the server (cron or a small container on another machine): the API token controls
the whole netcup account, it can stop servers and revert snapshots.

Usage: netcup-snapshot.py <command> [config.json]   (or set NETCUP_SNAPSHOT_CONFIG)

  login          device-code sign-in; stores the refresh token in the config, then lists servers
  list           list the account's servers and the configured server's snapshots
  check          dry-run: would an online snapshot work right now?
  once           take today's snapshot now and prune (same as the nightly run)
  daemon         sleep until the configured time every day, then run `once`

snapshot.log and status.json are written next to the config file.
Only snapshots named <prefix>YYYYMMDD are ever deleted; other snapshots are left alone.
Online snapshots only: if netcup refuses one, the run fails and the server is NOT shut down.
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REALM = "https://www.servercontrolpanel.de/realms/scp/protocol/openid-connect"
API = "https://www.servercontrolpanel.de/scp-core/api/v1"
CLIENT = "scp"
DEFAULTS = {"time": "02:30", "keep": 7, "prefix": "daily-"}
CONFIG_FILE = LOG_FILE = STATUS_FILE = None  # set in main() from the config path


class Failure(Exception):
    pass


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def write_private(path, text):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def config():
    with open(CONFIG_FILE) as f:
        return {**DEFAULTS, **json.load(f)}


def save_refresh_token(token):
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    cfg["refresh_token"] = token
    write_private(CONFIG_FILE, json.dumps(cfg, indent=2) + "\n")


def server_id(cfg):
    sid = cfg.get("server_id")
    if not isinstance(sid, int):
        raise Failure("set server_id in the config (the number `list` shows)")
    return sid


# ---------- HTTP ----------

def http(method, url, data=None, form=None, token=None, timeout=60):
    headers = {"Accept": "application/json"}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw.strip() else None
            except ValueError:
                payload = raw.decode(errors="replace")[:300]
            if e.code >= 500 and attempt < 2:
                time.sleep(10)
                continue
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 2:
                time.sleep(10)
                continue
            raise Failure(f"network error on {method} {url}: {e}")


class Session:
    """Access token from the stored refresh token; re-fetched after 4 minutes."""

    def __init__(self):
        self.access, self.at = None, 0

    def token(self):
        if self.access and time.time() - self.at < 240:
            return self.access
        refresh = config().get("refresh_token")
        if not refresh:
            raise Failure("no refresh token; run `login` first")
        code, resp = http("POST", REALM + "/token",
                          form={"client_id": CLIENT, "grant_type": "refresh_token", "refresh_token": refresh})
        if code != 200 or not isinstance(resp, dict) or "access_token" not in resp:
            err = resp.get("error_description") or resp.get("error") if isinstance(resp, dict) else resp
            raise Failure(f"token refresh failed (HTTP {code}: {err}); run `login` again")
        if resp.get("refresh_token") and resp["refresh_token"] != refresh:
            save_refresh_token(resp["refresh_token"])
        self.access, self.at = resp["access_token"], time.time()
        return self.access

    def api(self, method, path, data=None, ok=(200, 202, 204)):
        code, resp = http(method, API + path, data=data, token=self.token())
        if code not in ok:
            raise Failure(f"{method} {path} -> HTTP {code}: {json.dumps(resp)[:400]}")
        return code, resp

    def wait_task(self, task, what, limit=1800):
        uuid = (task or {}).get("uuid")
        if not uuid:
            raise Failure(f"{what}: no task id in answer: {json.dumps(task)[:300]}")
        deadline = time.time() + limit
        while time.time() < deadline:
            _, t = self.api("GET", f"/tasks/{uuid}")
            state = t.get("state")
            if state == "FINISHED":
                return t
            if state in ("ERROR", "CANCELED", "ROLLBACK", "WAITING_FOR_CANCEL"):
                raise Failure(f"{what}: task ended {state}: {t.get('message') or json.dumps(t.get('responseError'))}")
            time.sleep(10)
        raise Failure(f"{what}: task not finished after {limit // 60} min")


# ---------- commands ----------

def cmd_login():
    code, resp = http("POST", REALM + "/auth/device", form={"client_id": CLIENT, "scope": "offline_access openid"})
    if code != 200:
        code, resp = http("POST", REALM + "/auth/device", form={"client_id": CLIENT, "scope": "openid"})
    if code != 200:
        raise Failure(f"device login start failed: HTTP {code} {resp}")
    print("OPEN:", resp.get("verification_uri_complete") or resp.get("verification_uri"), flush=True)
    print("CODE:", resp.get("user_code"), flush=True)
    print(f"(valid {resp.get('expires_in')} s)", flush=True)
    interval = int(resp.get("interval") or 5)
    deadline = time.time() + int(resp.get("expires_in") or 600)
    while time.time() < deadline:
        time.sleep(interval)
        c, t = http("POST", REALM + "/token", form={
            "client_id": CLIENT, "device_code": resp["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if c == 200 and "refresh_token" in t:
            save_refresh_token(t["refresh_token"])
            kind = "offline" if "offline_access" in (t.get("scope") or "") else "session"
            log(f"login ok, refresh token stored ({kind} token)")
            return cmd_list()
        err = t.get("error") if isinstance(t, dict) else t
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            raise Failure(f"device login failed: {err}")
    raise Failure("device login expired")


def snapshots(s, sid):
    _, snaps = s.api("GET", f"/servers/{sid}/snapshots")
    return snaps or []


def cmd_list():
    s = Session()
    _, servers = s.api("GET", "/servers")
    for srv in servers or []:
        print(f"server {srv.get('id')}  {srv.get('name')}  {srv.get('nickname') or ''}", flush=True)
    sid = config().get("server_id")
    if isinstance(sid, int):
        for snap in snapshots(s, sid):
            print(f"  snapshot {snap.get('name')}  {snap.get('creationTime')}  online={snap.get('online')}"
                  f"  state={snap.get('state')}", flush=True)


def dry_run(s, sid):
    code, resp = http("POST", f"{API}/servers/{sid}/snapshots:dryrun", data={"onlineSnapshot": True},
                      token=s.token())
    if code == 200 and not resp:
        return None
    return f"HTTP {code}: {json.dumps(resp)[:400]}"


def cmd_check():
    problem = dry_run(Session(), server_id(config()))
    print("online snapshot possible" if problem is None else "online snapshot NOT possible: " + problem)


def status(result, detail):
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    write_private(STATUS_FILE, json.dumps({"time": now, "result": result, "detail": detail}) + "\n")


def cmd_once():
    cfg = config()
    try:
        sid = server_id(cfg)
        s = Session()
        name = cfg["prefix"] + time.strftime("%Y%m%d")
        names = {x.get("name") for x in snapshots(s, sid)}
        if name in names:
            log(f"{name} already exists, nothing to take")
        else:
            problem = dry_run(s, sid)
            if problem:
                raise Failure(f"online snapshot refused ({problem}); server left running, no snapshot taken")
            log(f"taking online snapshot {name}")
            _, task = s.api("POST", f"/servers/{sid}/snapshots", data={
                "name": name, "description": "nightly, automatic (netcup-snapshot)", "onlineSnapshot": True})
            t0 = time.time()
            s.wait_task(task, f"snapshot {name}")
            if name not in {x.get("name") for x in snapshots(s, sid)}:
                raise Failure(f"task finished but {name} is not in the snapshot list")
            log(f"snapshot {name} done in {int(time.time() - t0)} s")
        pat = re.compile(re.escape(cfg["prefix"]) + r"\d{8}$")
        ours = sorted(x.get("name") for x in snapshots(s, sid) if pat.fullmatch(x.get("name") or ""))
        for old in ours[:-int(cfg["keep"])] if len(ours) > int(cfg["keep"]) else []:
            log(f"deleting old snapshot {old}")
            _, task = s.api("DELETE", f"/servers/{sid}/snapshots/{old}")
            s.wait_task(task, f"delete {old}", limit=900)
        kept = sorted(x.get("name") for x in snapshots(s, sid))
        msg = f"snapshots now: {', '.join(kept)}"
        log("ok, " + msg)
        status("ok", msg)
        return 0
    except Failure as e:
        log(f"FAILED: {e}")
        status("FAILED", str(e))
        return 1


def cmd_daemon():
    log(f"daemon started, nightly at {config()['time']} ({time.strftime('%Z')})")
    while True:
        hh, mm = map(int, config()["time"].split(":"))
        now = time.time()
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        if target <= now:
            lt2 = time.localtime(now + 86400)
            target = time.mktime((lt2.tm_year, lt2.tm_mon, lt2.tm_mday, hh, mm, 0, 0, 0, -1))
        time.sleep(max(1, target - now))
        cmd_once()
        time.sleep(90)


def main():
    global CONFIG_FILE, LOG_FILE, STATUS_FILE
    cmds = {"login": cmd_login, "list": cmd_list, "check": cmd_check, "once": cmd_once, "daemon": cmd_daemon}
    args = sys.argv[1:]
    path = args[1] if len(args) == 2 else os.environ.get("NETCUP_SNAPSHOT_CONFIG")
    if len(args) not in (1, 2) or args[0] not in cmds or not path:
        sys.exit(__doc__)
    if not os.path.isfile(path):
        sys.exit(f"config file not found: {path}")
    CONFIG_FILE = os.path.abspath(path)
    LOG_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "snapshot.log")
    STATUS_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "status.json")
    os.umask(0o077)
    try:
        sys.exit(cmds[args[0]]() or 0)
    except Failure as e:
        log(f"FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
