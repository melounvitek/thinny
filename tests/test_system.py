import io
import sys
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


def test_a_hanging_command_times_out():
    with pytest.raises(CommandError, match="timed out after 1 s"):
        system.run(python("import time; time.sleep(30)"), timeout=1)


def test_a_failure_shows_the_tail_of_the_output():
    with pytest.raises(CommandError, match="exit 3\nboom"):
        system.run(python("import sys; print('boom'); sys.exit(3)"))


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
