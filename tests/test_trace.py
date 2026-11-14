import datetime as dt
import http.server
import json
import sys
import threading

import pytest

from thinny import system, trace
from thinny.registry import Instance, Registry


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
    monkeypatch.setattr(trace, "UNSENT", tmp_path / "unsent")
    reg = Registry({name: Instance(name, 9120 + i, "local", state="active") for i, name in enumerate(("alice", "bob"))})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
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


class Script(http.server.BaseHTTPRequestHandler):
    """A sheet's Apps Script web app as Google serves it: the POST gets a redirect, the script's answer
    comes at the address it points to."""
    def do_POST(self):
        sheet = self.server.sheet
        row = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if sheet.answer == "ok":
            sheet.rows.append(row)
        self.send_response(302)
        self.send_header("Location", f"/echo?answer={sheet.answer}")
        self.end_headers()

    def do_GET(self):
        answer = self.path.split("answer=")[1].encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


@pytest.fixture
def sheet():
    """A Google Sheet with its script (README): `rows` it got; `answer` other than "ok" plays a broken one."""
    server = http.server.HTTPServer(("127.0.0.1", 0), Script)
    url = f"http://127.0.0.1:{server.server_port}/exec"
    server.sheet = type("Sheet", (), {"rows": [], "answer": "ok", "url": url})()
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    yield server.sheet
    server.shutdown()


def test_an_instance_with_a_sheet_gets_each_entry_as_a_row(traced, tmp_path, sheet):
    Registry.load().get("alice").trace_sheet = sheet.url
    traced(event("instance-exec", command=["su", "-l"]), event("instance-console", type="console"),
           event("instance-exec", "bob", command=["su", "-l"]))   # bob has no sheet
    assert [(row["instance"], row["entry"]) for row in sheet.rows] == \
        [("alice", "incus exec alice -- su -l"), ("alice", "incus console alice")]
    # the same time as in its file
    assert [f"{row['time']} {row['entry']}" for row in sheet.rows] == \
        (tmp_path / "alice/admin-access.log").read_text().splitlines()
    assert not (tmp_path / "unsent/alice").exists()


@pytest.mark.parametrize("answer", ["", "<!DOCTYPE html><title>Sign in</title>"])   # down; not open to anyone
def test_rows_a_sheet_did_not_take_wait_and_go_first_next_time(traced, tmp_path, sheet, capsys, answer):
    Registry.load().get("alice").trace_sheet = sheet.url
    sheet.answer = answer
    traced(event("instance-exec", command=["true"]), event("instance-exec", command=["false"]))
    assert sheet.rows == [] and "rows waiting for its sheet: 2" in capsys.readouterr().err

    sheet.answer = "ok"
    traced(event("instance-exec", command=["id"]))
    assert [row["entry"] for row in sheet.rows] == \
        ["incus exec alice -- true", "incus exec alice -- false", "incus exec alice -- id"]
    assert not (tmp_path / "unsent/alice").exists()


def test_a_restarted_trace_sends_the_rows_that_wait(traced, sheet):
    Registry.load().get("alice").trace_sheet = sheet.url
    sheet.answer = ""
    traced(event("instance-exec", command=["true"]))
    sheet.answer = "ok"
    traced()   # e.g. after a reboot, with nothing new
    assert [row["entry"] for row in sheet.rows] == ["incus exec alice -- true"]
