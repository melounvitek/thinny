import io
import os
import signal
import sys
import threading
import time

import pytest

from thinny import system
from thinny.system import CommandError


def python(code):
    return [sys.executable, "-c", code]


def test_output_and_input_pass_through():
    res = system.run(python("import sys; print(sys.stdin.read().upper()); print('err', file=sys.stderr)"), input="hi")
    assert (res.returncode, res.stdout, res.stderr) == (0, "HI\n", "err\n")


def test_endless_output_is_cut_off():
    with pytest.raises(CommandError, match="more than 4000000 bytes of output"):
        system.run(python("import sys\nwhile True: sys.stdout.write('x' * 65536)"), timeout=30)


def test_endless_output_is_cut_off_at_once_also_when_its_own_process_holds_its_errors():
    holds = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], " \
            "stdout=subprocess.DEVNULL)\nwhile True: sys.stdout.write('x' * 65536)"
    start = time.monotonic()
    with pytest.raises(system.OutputTooLarge):
        system.run_bytes(python(holds), timeout=30)
    assert time.monotonic() - start < 1.5


def test_a_hanging_command_times_out():
    with pytest.raises(CommandError, match="timed out after 1 s"):
        system.run(python("import time; time.sleep(30)"), timeout=1)


def test_a_failure_shows_the_tail_of_the_output():
    with pytest.raises(CommandError, match="exit 3\nboom"):
        system.run(python("import sys; print('boom'); sys.exit(3)"))


def test_output_can_go_into_a_file_while_errors_stay_bounded():
    chunks = []
    code, out, err = system.run_bytes(python("import sys; sys.stdout.write('x' * 5_000_000); print('e', file=sys.stderr)"),
                                      into=chunks.append)
    assert (code, out, err, b"".join(chunks)) == (0, b"", b"e\n", b"x" * 5_000_000)   # past OUTPUT_LIMIT
    with pytest.raises(CommandError, match="more than 4000000 bytes of output"):
        system.run_bytes(python("import sys\nwhile True: sys.stderr.write('x' * 65536)"), into=chunks.append, timeout=30)


def gone(pid):
    """Ended and reaped, not left a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def endless(pid_file):
    """A command that notes its process, then writes forever."""
    return python(f"import os, sys\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                  "while True: sys.stdout.write('x' * 65536)")


def test_what_the_file_raises_stops_the_command_at_once(tmp_path):
    written = []

    def into(chunk):
        written.append(chunk)
        if sum(map(len, written)) > 1_000_000:
            raise CommandError("the disk is full")
    start = time.monotonic()
    with pytest.raises(CommandError, match="the disk is full"):
        system.run_bytes(endless(tmp_path / "pid"), into=into, timeout=60)
    assert time.monotonic() - start < 10
    assert gone(int((tmp_path / "pid").read_text()))


def test_what_the_file_raises_stops_the_command_also_when_its_own_process_holds_its_errors():
    holds = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], " \
            "stdout=subprocess.DEVNULL)\nwhile True: sys.stdout.write('x' * 65536)"

    def into(chunk):
        raise KeyboardInterrupt   # anything
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        system.run_bytes(python(holds), into=into, timeout=30)
    assert time.monotonic() - start < 3


def test_ctrl_c_stops_the_command(tmp_path):
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            system.run_bytes(python(f"import os, time; open({str(tmp_path / 'pid')!r}, 'w').write(str(os.getpid())); "
                                    "time.sleep(60)"), into=lambda chunk: None)
    finally:
        timer.cancel()
    assert gone(int((tmp_path / "pid").read_text()))


def test_nothing_reaches_the_file_after_a_command_that_timed_out(tmp_path, monkeypatch):
    """Its own process, which outlives it, keeps writing."""
    errors, chunks = [], []
    monkeypatch.setattr(threading, "excepthook", errors.append)
    later = "import sys, time; time.sleep(1); sys.stdout.write('late'); sys.stdout.flush()"
    command = python(f"import os, subprocess, sys, time; open({str(tmp_path / 'pid')!r}, 'w').write(str(os.getpid())); "
                     f"print('first', flush=True); subprocess.Popen([sys.executable, '-c', {later!r}]); time.sleep(60)")
    start = time.monotonic()
    with pytest.raises(CommandError, match="timed out after 0 s"):
        system.run_bytes(command, into=chunks.append, timeout=0.5)
    assert time.monotonic() - start < 1
    assert gone(int((tmp_path / "pid").read_text()))
    time.sleep(1.5)   # until its own process wrote
    assert (b"".join(chunks), errors) == (b"first\n", [])


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_a_terminal_sees_a_long_step_counting_up_on_one_line(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    monkeypatch.setattr(sys, "stderr", Terminal())
    with system.step("alice: system packages"):
        for _ in range(100):   # until the first update, at most 5 s
            if sys.stderr.getvalue().count("\r") > 1:
                break
            time.sleep(0.05)
    output = sys.stderr.getvalue()
    frames = output.split("\r")[1:]
    assert frames[0] == "alice: system packages... 0:00"
    assert frames[1] in ("alice: system packages... 0:01", "alice: system packages... 0:02")   # or on a busy machine
    assert output.endswith("\n") and output.count("\n") == 1   # one line, ended with the step


def test_the_log_gets_when_a_step_started_and_how_long_it_took(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    with system.step("alice: system packages"):
        pass
    with pytest.raises(CommandError), system.step("alice: gateway"):
        raise CommandError("boom")
    lines = ["alice: system packages...", "alice: system packages: done in 0:00",
             "alice: gateway...", "alice: gateway: failed after 0:00"]
    assert capsys.readouterr().err.splitlines() == lines   # without a terminal, as the nightly update
    assert [line.split(" ", 2)[2] for line in (tmp_path / "log").read_text().splitlines()] == lines
