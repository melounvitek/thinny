import sys

import pytest

from hermes_manager import system
from hermes_manager.system import CommandError


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
