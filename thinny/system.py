"""Running commands as root on the host and as an instance's account in its container, and small host facts."""

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
import threading
import time
from pathlib import Path

STATE_DIR = Path("/var/lib/thinny")
LOG_FILE = Path("/var/log/thinny.log")
LOCK_FILE = Path("/run/thinny.lock")
INSTANCE_UID = 1000        # every instance's account inside its container
OUTPUT_LIMIT = 4_000_000   # bytes per output stream of one command


class CommandError(Exception):
    pass


class OutputTooLarge(CommandError):
    pass


def log(message: str, *, echo: bool = True) -> None:
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    if echo:
        print(message, file=sys.stderr, flush=True)
    with contextlib.suppress(OSError), LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


@contextlib.contextmanager
def step(message: str):
    """A long step: a terminal shows `message... 1:23` with the time so far; the log gets its start and
    duration."""
    start = time.monotonic()
    live = sys.stderr.isatty()
    done = threading.Event()

    def elapsed() -> str:
        seconds = int(time.monotonic() - start)
        return f"{seconds // 60}:{seconds % 60:02}"

    def show(end: str = "") -> None:
        print(f"\r{message}... {elapsed()}", end=end, file=sys.stderr, flush=True)

    def tick() -> None:
        while not done.wait(1):
            show()

    log(f"{message}...", echo=not live)
    ticker = threading.Thread(target=tick, daemon=True)
    if live:
        show()
        ticker.start()
    result = "failed after"
    try:
        yield
        result = "done in"
    finally:
        done.set()
        if live:
            ticker.join()
            show("\n")
        log(f"{message}: {result} {elapsed()}", echo=not live)


def run_bytes(cmd: list[str], *, input: bytes | None = None, timeout: float = 600, env: dict | None = None,
              cwd: str | Path | None = None, limit: int = OUTPUT_LIMIT) -> tuple[int, bytes, bytes]:
    """(exit code, stdout, stderr) of a command. An instance's users control every program in its
    container, so the output is bounded and the timeout covers it: past either, the command is
    killed and CommandError raised."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd)
    out, err, too_large = bytearray(), bytearray(), []

    def drain(pipe, buf):
        while chunk := pipe.read1(65536):
            buf += chunk
            if len(buf) > limit:
                too_large.append(True)
                proc.kill()
                return

    def feed():
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.write(input)
            proc.stdin.close()

    threads = [threading.Thread(target=drain, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, err), daemon=True)]
    if input is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        code = proc.wait(max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        code = None
    if code is None or any(thread.is_alive() for thread in threads):
        proc.kill()
        raise CommandError(f"{' '.join(cmd)}: timed out after {timeout:.0f} s")
    proc.stdout.close()
    proc.stderr.close()
    if too_large:
        raise OutputTooLarge(f"{' '.join(cmd)}: more than {limit} bytes of output")
    return code, bytes(out), bytes(err)


def run(cmd: list[str], *, check: bool = True, input: str | None = None, timeout: float = 600,
        env: dict | None = None, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run a command with captured text output; raise CommandError with its tail on failure."""
    code, out, err = run_bytes(cmd, input=None if input is None else input.encode(), timeout=timeout, env=env, cwd=cwd)
    res = subprocess.CompletedProcess(cmd, code, out.decode(errors="replace"), err.decode(errors="replace"))
    if check and code != 0:
        tail = "\n".join((res.stdout + res.stderr).strip().splitlines()[-15:])
        raise CommandError(f"{' '.join(cmd)} -> exit {code}\n{tail}")
    return res


def user_env(name: str) -> dict[str, str]:
    runtime = f"/run/user/{INSTANCE_UID}"
    return {"HOME": f"/home/{name}", "USER": name, "LOGNAME": name,
            "PATH": f"/home/{name}/.local/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


def as_user_cmd(name: str, cmd: list[str]) -> list[str]:
    """`cmd` as the instance's account in its container, from its home, in a clean environment.
    (`incus exec --user` would leave out its groups.)"""
    return ["incus", "exec", name, "--cwd", f"/home/{name}", "--", "runuser", "-u", name, "--", "env", "-i",
            *[f"{k}={v}" for k, v in user_env(name).items()], *cmd]


def as_user(name: str, cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return run(as_user_cmd(name, cmd), **kw)


def in_container(name: str, cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """`cmd` as root inside the instance's container."""
    return run(["incus", "exec", name, "--", *cmd], **kw)


def as_user_interactive(name: str, cmd: list[str]) -> int:
    """Run as the instance's account with the admin's terminal attached (device logins)."""
    return subprocess.run(as_user_cmd(name, cmd)).returncode


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


def read_user_file(name: str, path: str, limit: int = 1_000_000) -> str:
    """A file in an instance's container, read as its account and bounded (it could point at
    /dev/zero). Empty when unreadable."""
    try:
        code, out, _ = run_bytes(as_user_cmd(name, ["cat", path]), timeout=30, limit=limit)
    except OutputTooLarge:
        raise CommandError(f"{path} of {name} is larger than {limit} bytes") from None
    if code != 0:
        return ""
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise CommandError(f"{path} of {name} is not UTF-8 text") from None


def write_user_file(name: str, path: str, content: str) -> None:
    """Write a private file as the instance's account (as root it could follow a planted symlink)."""
    as_user(name, ["sh", "-c", 'umask 077 && cat > "$1.tmp" && mv -f "$1.tmp" "$1"', "sh", path], input=content)


@contextlib.contextmanager
def lock(path: Path | None = None):
    """One thinny change at a time (commands and the nightly update); another `path` locks something else."""
    with (path or LOCK_FILE).open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("another thinny run is in progress") from None
        yield


def uid_of(name: str) -> int | None:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


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


def default_route() -> tuple[str, str]:
    """The interface and source address the server reaches the internet with ("" when unknown)."""
    words = run(["ip", "-4", "route", "get", "1.1.1.1"], check=False, timeout=10).stdout.split()
    return tuple(words[words.index(key) + 1] if key in words[:-1] else "" for key in ("dev", "src"))
