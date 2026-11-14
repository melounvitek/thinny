import contextlib
import datetime as dt
import http.server
import json
import re
import sys
import threading
import time

import pytest

from thinny import system, trace
from thinny.registry import Instance, Registry, RegistryError
from thinny.system import CommandError

GOOGLE = trace.SHEET_URL   # before any test replaces it


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
        sheet.tries += 1
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


@contextlib.contextmanager
def google_sheet():
    """A Google Sheet with its script (README): `rows` it took, `tries` to add one; `answer` other than "ok"
    plays a broken one."""
    server = http.server.HTTPServer(("127.0.0.1", 0), Script)
    url = f"http://127.0.0.1:{server.server_port}/exec"
    server.sheet = type("Sheet", (), {"rows": [], "tries": 0, "answer": "ok", "url": url})()
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    try:
        yield server.sheet
    finally:
        server.shutdown()


@pytest.fixture
def sheet():
    with google_sheet() as found:
        yield found


def waiting(tmp_path, name):
    return sorted((tmp_path / "unsent" / name).glob("*.json"))


def test_an_instance_with_a_sheet_gets_each_entry_as_a_row(traced, tmp_path, sheet):
    Registry.load().get("alice").trace_sheet = sheet.url
    traced(event("instance-exec", command=["su", "-l"]), event("instance-console", type="console"),
           event("instance-exec", "bob", command=["su", "-l"]))   # bob has no sheet
    assert not waiting(tmp_path, "alice") and not waiting(tmp_path, "bob")
    assert [(row["instance"], row["entry"]) for row in sheet.rows] == \
        [("alice", "incus exec alice -- su -l"), ("alice", "incus console alice")]
    # the same time as in its file
    assert [f"{row['time']} {row['entry']}" for row in sheet.rows] == \
        (tmp_path / "alice/admin-access.log").read_text().splitlines()


@pytest.mark.parametrize("answer", ["", "<!DOCTYPE html><title>Sign in</title>"])   # down; not open to anyone
def test_rows_a_sheet_did_not_take_wait_and_go_first(traced, tmp_path, sheet, answer):
    Registry.load().get("alice").trace_sheet = sheet.url
    sheet.answer = answer
    traced(event("instance-exec", command=["true"]), event("instance-exec", command=["false"]))
    assert sheet.tries and sheet.rows == [] and len(waiting(tmp_path, "alice")) == 2

    sheet.answer = "ok"
    traced(event("instance-exec", command=["id"]))   # or the trace's start, or its next try
    assert [row["entry"] for row in sheet.rows] == \
        ["incus exec alice -- true", "incus exec alice -- false", "incus exec alice -- id"]
    assert not waiting(tmp_path, "alice")


def test_rows_wait_while_the_registry_cannot_be_read(traced, tmp_path, sheet, monkeypatch):
    reg = Registry.load()
    reg.get("alice").trace_sheet = sheet.url
    tried = []

    def broken(cls, path=None):
        tried.append(True)
        raise ValueError("Expecting value: line 1 column 1 (char 0)")
    monkeypatch.setattr(Registry, "load", classmethod(broken))
    traced(event("instance-exec", command=["true"]))
    assert tried and len(waiting(tmp_path, "alice")) == 1

    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    traced()
    assert [row["entry"] for row in sheet.rows] == ["incus exec alice -- true"]


def test_rows_keep_their_order_when_the_clock_steps_back(traced, sheet, monkeypatch):
    Registry.load().get("alice").trace_sheet = sheet.url
    sheet.answer = ""   # they wait, so their order is the files'
    clock = iter(range(2 * 10**18, 10**18, -10**9))   # a second back at every reading
    monkeypatch.setattr(time, "time_ns", lambda: next(clock))
    traced(event("instance-exec", command=["true"]), event("instance-exec", command=["false"]))
    sheet.answer = "ok"
    traced()
    assert [row["entry"] for row in sheet.rows] == ["incus exec alice -- true", "incus exec alice -- false"]


def test_the_sheet_decides_for_each_row_as_the_registry_is_then(tmp_path, sheet, monkeypatch):
    """`instance trace-sheet` may change the registry while the trace sends."""
    monkeypatch.setattr(trace, "UNSENT", tmp_path)
    connected = Registry({"alice": Instance("alice", 9120, "local", state="active", trace_sheet=sheet.url)})
    states = iter([Registry(connected.instances | {"alice": Instance("alice", 9120, "local", state="active")}),
                   connected, Registry({"alice": Instance("alice", 9120, "local", state="active")})])

    def load(cls, path=None):
        reg = next(states)
        if reg is connected:   # a row written after its sheet was connected
            (tmp_path / "alice/3.json").write_text(json.dumps({"entry": "after"}))
        return reg
    monkeypatch.setattr(Registry, "load", classmethod(load))
    (tmp_path / "alice").mkdir()
    for row, entry in (("1", "before"), ("2", "connected")):
        (tmp_path / "alice" / f"{row}.json").write_text(json.dumps({"entry": entry}))
    trace.send_waiting("alice")   # no sheet yet for 1, its sheet for 2, off again for 3
    assert [row["entry"] for row in sheet.rows] == ["connected"]
    assert sorted(path.name for path in (tmp_path / "alice").iterdir()) == ["3.json"]   # not listed this round


def test_the_trace_goes_on_when_rows_for_sheets_cannot_be_kept(traced, tmp_path, capsys):
    (tmp_path / "unsent").write_text("")   # not a directory: no row can be kept there
    traced(event("instance-exec", command=["true"]), event("instance-exec", command=["false"]))
    assert (tmp_path / "alice/admin-access.log").read_text().count("incus exec alice") == 2
    assert "alice: its sheet misses this entry" in capsys.readouterr().err


@pytest.fixture
def sheets(tmp_path, monkeypatch):
    """Two sheets thinny accepts (only Apps Script URLs in production), and the registry of alice and bob."""
    with google_sheet() as first, google_sheet() as second:
        monkeypatch.setattr(trace, "SHEET_URL", re.compile(f"{re.escape(first.url)}|{re.escape(second.url)}"))
        monkeypatch.setattr(trace, "UNSENT", tmp_path / "unsent")
        monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
        reg = Registry({"alice": Instance("alice", 9120, "local", state="active"),
                        "bob": Instance("bob", 9121, "local", state="disabled")})
        yield reg, first, second


def entries(sheet):
    return [(row["instance"], row["entry"]) for row in sheet.rows]


def test_a_sheet_is_connected_only_once_it_took_a_test_row(sheets):
    reg, first, _ = sheets
    first.answer = "<!DOCTYPE html><title>Sign in</title>"   # not open to anyone
    with pytest.raises(CommandError, match="is it open to anyone"):
        trace.connect_sheet(reg, "alice", first.url)
    assert reg.get("alice").trace_sheet == ""

    first.answer = "ok"
    trace.connect_sheet(reg, "alice", first.url)
    assert reg.get("alice").trace_sheet == first.url
    assert entries(first) == [("alice", "thinny sends this instance's access trace here from now on")]


@pytest.mark.parametrize("url", ["http://192.168.1.1/exec", "https://script.google.com.example.org/macros/s/x/exec",
                                 "https://script.google.com/macros/s/x/exec/../../../../a", "https://example.org/"])
def test_only_a_google_apps_script_url_is_taken(sheets, monkeypatch, url):
    reg, _, _ = sheets
    monkeypatch.setattr(trace, "SHEET_URL", GOOGLE)
    with pytest.raises(CommandError, match="https://script.google.com/macros/s/"):
        trace.connect_sheet(reg, "alice", url)
    assert reg.get("alice").trace_sheet == ""
    assert trace.SHEET_URL.fullmatch("https://script.google.com/macros/s/AKfycbx-1_Z/exec")


def test_a_sheet_that_stops_getting_the_trace_is_told(sheets, tmp_path):
    reg, first, second = sheets
    trace.connect_sheet(reg, "alice", first.url)
    trace.connect_sheet(reg, "alice", second.url)
    assert entries(first)[-1] == ("alice", "thinny stopped sending this instance's access trace here")
    assert entries(second) == [("alice", "thinny sends this instance's access trace here from now on")]

    trace.connect_sheet(reg, "alice", "off")
    assert reg.get("alice").trace_sheet == ""
    assert entries(second)[-1] == ("alice", "thinny stopped sending this instance's access trace here")


def test_a_disabled_instance_gets_no_sheet(sheets):
    reg, first, _ = sheets
    with pytest.raises(RegistryError, match="bob is disabled"):
        trace.connect_sheet(reg, "bob", first.url)
    assert first.rows == []
