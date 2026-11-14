import datetime as dt
import json
import sys

import pytest

from thinny import system, trace


def event(action, name="alice", **context):
    return {"type": "lifecycle", "timestamp": "2026-11-14T10:23:45.123456789+01:00",
            "metadata": {"action": action, "source": f"/1.0/instances/{name}", "name": name, "project": "default",
                         "context": context, "requestor": {"username": "root", "protocol": "unix", "address": "@"}}}


def exec_event(cmd):
    """The event of an `incus exec` command line."""
    return event("instance-exec", cmd[2], command=cmd[cmd.index("--") + 1:])


@pytest.fixture
def traced(tmp_path, monkeypatch):
    """alice and bob are instances; plays `incus monitor` with the events given, printed as Incus does."""
    monkeypatch.setattr(trace, "DIR", tmp_path)
    for name in ("alice", "bob"):
        (tmp_path / name).mkdir()

    def monitor(*events):
        text = "".join(json.dumps(e) + "\n\n" for e in events)
        monkeypatch.setattr(trace, "MONITOR", [sys.executable, "-c", f"print({text!r}, end='')"])
        assert trace.run() == 1   # the monitor ended: systemd starts the trace again
    return monitor


def test_an_admins_shell_exec_and_console_are_traced_for_the_host_and_the_instance(traced, tmp_path, capsys):
    traced(event("instance-exec", command=["su", "-l"]),   # incus shell alice
           event("instance-exec", command=["runuser", "-l", "alice"]),
           event("instance-console", type="console"),
           event("instance-exec", "bob", command=["cat", "/home/bob/.hermes/.env"]),
           event("instance-exec", "other", command=["bash"]))   # not an instance
    journal = ["incus exec alice -- su -l", "incus exec alice -- runuser -l alice", "incus console alice",
               "incus exec bob -- cat /home/bob/.hermes/.env", "incus exec other -- bash"]
    assert capsys.readouterr().out.splitlines() == journal
    alice = (tmp_path / "alice/admin-access.log").read_text().splitlines()
    assert [line.split(" ", 1)[1] for line in alice] == journal[:3]
    assert dt.datetime.fromisoformat(alice[0].split(" ")[0]).utcoffset() is not None
    assert (tmp_path / "bob/admin-access.log").read_text().endswith(" incus exec bob -- cat /home/bob/.hermes/.env\n")
    assert not (tmp_path / "other").exists()


def test_thinnys_own_commands_and_other_events_are_not_traced(traced, tmp_path, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(system, "run", lambda cmd, **kw: ran.append(cmd))
    system.in_container("alice", ["systemctl", "is-system-running"])
    traced(exec_event(ran[0]), exec_event(system.as_user_cmd("alice", ["hermes", "status"])),
           event("instance-started"), event("instance-console-retrieved"))
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "alice/admin-access.log").exists()


def test_a_command_stays_one_line_a_terminal_shows_as_it_is(traced, tmp_path):
    traced(event("instance-exec", command=["sh", "-c", "true\n2026-01-01T00:00:00+01:00 nothing\x1b[2J"]))
    assert (tmp_path / "alice/admin-access.log").read_text().split(" ", 1)[1] == \
        "incus exec alice -- sh -c 'true\\n2026-01-01T00:00:00+01:00 nothing\\x1b[2J'\n"
