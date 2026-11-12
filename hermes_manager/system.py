"""Running commands as root and as a person, and small host facts."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path("/var/lib/hermes-manager")
LOG_FILE = Path("/var/log/hermes-manager.log")
LOCK_FILE = Path("/run/hermes-manager.lock")
HERMES = "/usr/local/bin/hermes"
# HOME for root's Hermes runs: the official installer and `hermes update` publish launchers
# into ~/.local/bin, which must not replace root's own.
ROOT_HERMES_HOME_DIR = STATE_DIR / "root-home"


class CommandError(Exception):
    pass


def log(message: str) -> None:
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(message, file=sys.stderr, flush=True)
    with contextlib.suppress(OSError), LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: list[str], *, check: bool = True, input: str | None = None, timeout: float = 600,
        env: dict | None = None, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run a command with captured text output; raise CommandError with its tail on failure."""
    try:
        res = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout,
                             env=env, cwd=cwd, stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise CommandError(f"{' '.join(cmd)}: timed out after {timeout:.0f} s") from e
    if check and res.returncode != 0:
        tail = "\n".join((res.stdout + res.stderr).strip().splitlines()[-15:])
        raise CommandError(f"{' '.join(cmd)} -> exit {res.returncode}\n{tail}")
    return res


def user_env(name: str) -> dict[str, str]:
    pw = pwd.getpwnam(name)
    runtime = f"/run/user/{pw.pw_uid}"
    return {"HOME": pw.pw_dir, "USER": name, "LOGNAME": name, "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


def as_user_cmd(name: str, cmd: list[str]) -> list[str]:
    return ["runuser", "-u", name, "--", "env", "-i", *[f"{k}={v}" for k, v in user_env(name).items()], *cmd]


def as_user(name: str, cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run as the person, in a clean environment, from their home (never as root inside it)."""
    return run(as_user_cmd(name, cmd), cwd=pwd.getpwnam(name).pw_dir, **kw)


def as_user_interactive(name: str, cmd: list[str]) -> int:
    """Run as the person with the admin's terminal attached (device logins)."""
    return subprocess.run(as_user_cmd(name, cmd), cwd=pwd.getpwnam(name).pw_dir).returncode


def hermes_root(args: list[str], **kw) -> subprocess.CompletedProcess:
    """Root's `hermes` on the shared install, keeping /opt/hermes at 711."""
    ROOT_HERMES_HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
           "HOME": str(ROOT_HERMES_HOME_DIR), "HERMES_HOME": "/opt/hermes", "HERMES_HOME_MODE": "0711"}
    return run([HERMES, *args], env=env, cwd=ROOT_HERMES_HOME_DIR, **kw)


def write_file(path: Path, content: str, mode: int = 0o644) -> bool:
    """Atomically write a root-owned file; True when the content changed."""
    path = Path(path)
    with contextlib.suppress(FileNotFoundError):
        if path.read_text(encoding="utf-8") == content and (path.stat().st_mode & 0o777) == mode:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def write_user_file(name: str, path: str, content: str) -> None:
    """Write a private file as the person (a root write could follow a planted symlink)."""
    as_user(name, ["sh", "-c", 'umask 077 && cat > "$1.tmp" && mv -f "$1.tmp" "$1"', "sh", path], input=content)


@contextlib.contextmanager
def lock():
    """One hermes-manager change at a time (commands and the nightly update)."""
    with LOCK_FILE.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("another hermes-manager run is in progress") from None
        yield


def uid_of(name: str) -> int | None:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def slice_uids() -> list[int]:
    """Uids whose user slice exists now (nft can only match existing cgroups)."""
    uids = []
    for path in Path("/sys/fs/cgroup/user.slice").glob("user-*.slice"):
        with contextlib.suppress(ValueError):
            uids.append(int(path.name[len("user-"):-len(".slice")]))
    return sorted(uids)


def tailscale_status() -> dict | None:
    """`tailscale status --json` when Tailscale is installed and running, else None."""
    if not shutil.which("tailscale"):
        return None
    res = run(["tailscale", "status", "--json"], check=False, timeout=30)
    if res.returncode != 0:
        return None
    status = json.loads(res.stdout)
    return status if status.get("BackendState") == "Running" else None


def tailscale_name() -> str:
    status = tailscale_status()
    if not status:
        raise CommandError("Tailscale is not running on this server")
    return status["Self"]["DNSName"].rstrip(".")


def gateway_active(name: str) -> bool:
    res = as_user(name, ["systemctl", "--user", "is-active", "hermes-gateway"], check=False, timeout=30)
    return res.stdout.strip() == "active"


def http_status(url: str, timeout: float = 5) -> str:
    """HTTP status code as text ("000" when nothing answers), checked as root."""
    res = run(["curl", "-s", "-o", "/dev/null", "-m", str(timeout), "-w", "%{http_code}", url],
              check=False, timeout=timeout + 10)
    return res.stdout.strip() or "000"
