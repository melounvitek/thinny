import base64
import dataclasses
import hashlib
import io
import json
import os
import re
import subprocess
import time
import types

import pytest

from thinny import cli, dashboard, host, instances, render, system, update
from thinny.registry import Instance, Registry
from thinny.settings import Settings, SettingsError
from thinny.system import CommandError

S = Settings(model="m1", allowed_models=("m1",), access_default="local", lan_address="192.168.1.10")
ON = dataclasses.replace(S, dashboard_enabled=True)
USAGE = dataclasses.replace(ON, dashboard_subscription_usage=True)
HOUR = 1_800_000_000 // 3600 * 3600   # a clock hour's start
SERVICES = "prometheus.service prometheus-node-exporter.service grafana-server.service"
KEY = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGTnhmkBEADUfake\n-----END PGP PUBLIC KEY BLOCK-----\n"


class Server:
    """Plays dpkg, apt, systemd, openssl, Incus, btrfs and Tailscale for the dashboard; records the commands."""

    def __init__(self):
        self.calls = []
        self.installed = {"openssl"}
        self.source_at_update = None   # Grafana's apt source when apt-get update ran
        self.metrics_address = ""
        self.trust = [{"fingerprint": "c1", "type": "client", "name": "admin"}]
        self.masked, self.enabled = set(), set()
        self.served = set()
        self.tailscale = {"BackendState": "Running"}
        self.containers = {"alice": 30 * 2**30}   # name -> its disk quota
        self.stopped = set()   # containers not running
        # name -> its `hermes usage --json`; none: Hermes got nothing; an error: it did not finish
        self.usage = {}
        self.settings = ON   # config.toml's
        self.now = time.time()
        self.answers = []
        self.prompts = []
        self.fails = set()   # commands that fail, by their start
        self.starting = {}   # URL -> the HTTP status codes it answers with before 200, while its service starts
        self.clock = 0.0

    def http_status(self, url, timeout=5):
        self.calls.append(f"GET {url}")
        return self.starting[url].pop(0) if self.starting.get(url) else "200"

    def sleep(self, seconds):
        self.clock += seconds

    def run(self, cmd, *, check=True, **kw):
        self.calls.append(" ".join(cmd))
        out, code = self.answer(cmd)
        if check and code:
            raise CommandError(f"{' '.join(cmd)} -> exit {code}")
        return subprocess.CompletedProcess(cmd, code, out, "")

    def answer(self, cmd):
        if " ".join(cmd).startswith(tuple(self.fails)):
            return "", 1
        if cmd[0] == "dpkg-query":
            return "".join(f"{p} install ok installed\n" for p in cmd[3:] if p in self.installed), \
                int(not set(cmd[3:]) <= self.installed)
        if cmd[:2] == ["apt-get", "update"]:
            self.source_at_update = dashboard.GRAFANA_SOURCE.read_text()
        elif cmd[:2] == ["apt-get", "install"]:
            self.installed |= {arg for arg in cmd[2:] if not arg.startswith("-")}
        elif cmd[0] == "openssl":
            open(cmd[cmd.index("-keyout") + 1], "w").write("private key")
            der = base64.b64encode(b"certificate").decode()
            open(cmd[cmd.index("-out") + 1], "w").write(f"-----BEGIN CERTIFICATE-----\n{der}\n-----END CERTIFICATE-----\n")
        elif cmd[:4] == ["incus", "config", "get", "core.metrics_address"]:
            return self.metrics_address + "\n", 0
        elif cmd[:4] == ["incus", "config", "set", "core.metrics_address"]:
            self.metrics_address = cmd[4]
        elif cmd[:4] == ["incus", "config", "unset", "core.metrics_address"]:
            self.metrics_address = ""
        elif cmd[:4] == ["incus", "config", "trust", "list"]:
            return json.dumps(self.trust), 0
        elif cmd[:4] == ["incus", "config", "trust", "add-certificate"]:
            der = base64.b64decode(open(cmd[4]).read().splitlines()[1])
            self.trust.append({"fingerprint": hashlib.sha256(der).hexdigest(), "type": cmd[5].removeprefix("--type="),
                               "name": cmd[6].removeprefix("--name=")})
        elif cmd[:4] == ["incus", "config", "trust", "remove"]:
            self.trust = [entry for entry in self.trust if entry["fingerprint"] != cmd[4]]
        elif cmd[:2] == ["incus", "list"]:
            return json.dumps([{"name": name, "status": "Stopped" if name in self.stopped else "Running",
                                "state": {"disk": {"root": {"total": quota}}}}
                               for name, quota in self.containers.items()]), 0
        elif cmd[:2] == ["incus", "exec"]:   # as the instance's account
            assert cmd[-3:] == ["hermes", "usage", "--json"] and cmd[2] not in self.stopped
            answer = self.usage.get(cmd[2], "")
            if isinstance(answer, CommandError):
                raise answer
            return answer, 0 if answer else 1
        elif cmd[:3] == ["btrfs", "qgroup", "show"]:
            return f"Qgroupid Referenced Exclusive Path\n0/257 {2**30} {2**29} {cmd[-1]}\n", 0
        elif cmd[:2] == ["systemctl", "mask"]:
            self.masked |= {unit for unit in cmd[2:] if unit != "--now"}
            self.enabled -= self.masked
        elif cmd[:2] == ["systemctl", "unmask"]:
            self.masked -= set(cmd[2:])
        elif cmd[:2] == ["systemctl", "enable"]:
            units = {unit for unit in cmd[2:] if unit != "--now"}
            assert not units & self.masked
            self.enabled |= units
        elif cmd[:3] == ["systemctl", "disable", "--now"]:
            self.enabled -= set(cmd[3:])
        elif cmd[:2] == ["tailscale", "serve"] and cmd[-1] == "off":
            self.served.discard(int(cmd[2].removeprefix("--https=")))
        return "", 0

    def index(self, prefix):
        """Position of the first command starting with `prefix`."""
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def input(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class Terminal:
    def isatty(self):
        return True


def dashboard_files(tmp_path, monkeypatch):
    """The dashboard's files in tmp_path."""
    etc = tmp_path / "etc"
    for name, path in (("PROMETHEUS_CONFIG", etc / "thinny/prometheus.yml"), ("CERT", etc / "thinny/metrics.crt"),
                       ("KEY", etc / "thinny/metrics.key"), ("PASSWORD", etc / "thinny/dashboard.env"),
                       ("GRAFANA_KEY", etc / "apt/keyrings/thinny-grafana.asc"),
                       ("GRAFANA_SOURCE", etc / "apt/sources.list.d/thinny-grafana.list"),
                       ("GRAFANA_UPGRADES", etc / "apt/apt.conf.d/52thinny-grafana"),
                       ("PROVISIONING", etc / "grafana/provisioning"),
                       ("DISK_METRICS", tmp_path / "var/lib/prometheus/node-exporter/thinny.prom"),
                       ("USAGE_METRICS", tmp_path / "var/lib/prometheus/node-exporter/thinny-usage.prom"),
                       ("UNFINISHED", tmp_path / "var/lib/thinny/dashboard-unfinished")):
        monkeypatch.setattr(dashboard, name, path)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A server with alice, Tailscale running and 513 GB free; the admin at a terminal."""
    srv = Server()
    for module in (dashboard, instances, system):
        monkeypatch.setattr(module, "run", srv.run)
    monkeypatch.setattr(dashboard, "load", lambda: srv.settings)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)   # as root: cli.main would run itself again with sudo
    dashboard_files(tmp_path, monkeypatch)
    monkeypatch.setattr(update, "UNFINISHED", tmp_path / "update-unfinished")
    (tmp_path / "etc/thinny").mkdir(parents=True)
    monkeypatch.setattr(host, "SYSTEMD", tmp_path / "etc/systemd/system")
    monkeypatch.setattr(host, "DASHBOARD_STATE", tmp_path / "var/lib/thinny/dashboard")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    monkeypatch.setattr(host, "tailscale_status", lambda: srv.tailscale)
    monkeypatch.setattr(host, "tailscale_served_ports", lambda: set(srv.served))
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dashboard, "tailscale_name", lambda: "server.tailnet.ts.net")
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(KEY.encode()))
    monkeypatch.setattr(dashboard.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=513_000_000_000))
    monkeypatch.setattr(dashboard.sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", srv.input)
    monkeypatch.setattr(dashboard, "http_status", srv.http_status)
    monkeypatch.setattr(dashboard, "time", types.SimpleNamespace(monotonic=lambda: srv.clock, sleep=srv.sleep,
                                                                  time=lambda: srv.now))
    reg = Registry({"alice": Instance("alice", 9120, "lan", state="active")})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    srv.tmp = tmp_path
    return srv


def files(srv):
    return sorted(str(path.relative_to(srv.tmp)) for path in srv.tmp.rglob("*") if path.is_file() and path.name != "log")


def test_turning_it_on_tells_what_it_takes_and_a_no_changes_nothing(server, capsys):
    srv = server
    srv.answers = ["n"]
    dashboard.apply(ON, Registry.load())
    assert capsys.readouterr().out == (
        "The dashboard is on in config.toml, but not installed yet. Installing it:\n"
        "  - adds Grafana's apt repository (https://apt.grafana.com stable main) with its signing key, and lets\n"
        "    unattended upgrades update Grafana from it, new major versions included\n"
        "  - installs prometheus, prometheus-node-exporter, grafana (from Grafana's repository)\n"
        "  - downloads about 445 MB and takes about 2.2 GB of disk, and up to about 0.3 GB more for 30 days of history\n"
        "  - uses about 0.3 GB of RAM\n"
        "  - turns on Incus's metrics at 127.0.0.1:8444, for a certificate that can only read them\n"
        "  Free disk space: 513.0 GB\n"
        "Nothing installed or changed. To install it later: thinny config apply. Not to be asked again, set "
        "dashboard.enabled = false (thinny config edit).\n")
    assert srv.prompts == ["Install the dashboard? [y/N] "]
    assert srv.calls == ["incus config get core.metrics_address",
                         "dpkg-query -W -f=${Package} ${Status}\n prometheus prometheus-node-exporter grafana openssl"]
    assert files(srv) == []


def test_without_a_terminal_it_installs_nothing_and_says_how(server, monkeypatch, capsys):
    srv = server
    monkeypatch.setattr(dashboard.sys, "stdin", io.StringIO())
    dashboard.apply(ON, Registry.load())
    assert "the dashboard is not installed: thinny installs it only once you confirm. Run on a terminal: " \
           "thinny config apply" in capsys.readouterr().err
    assert srv.prompts == [] and not any(call.startswith(("apt-get", "systemctl", "incus config set"))
                                         for call in srv.calls)
    assert files(srv) == []


def test_once_confirmed_it_installs_and_starts_everything_on_127_0_0_1(server, capsys):
    srv = server
    srv.answers = ["y"]
    dashboard.apply(ON, Registry.load())
    # the packages start no service before thinny set them up
    assert srv.index(f"systemctl mask {SERVICES}") < srv.index("apt-get install")
    assert "apt-get install -y -q --no-install-recommends prometheus prometheus-node-exporter grafana" in srv.calls
    assert srv.source_at_update == render.grafana_source(str(dashboard.GRAFANA_KEY))
    assert dashboard.GRAFANA_KEY.read_text() == KEY
    assert "site=apt.grafana.com" in dashboard.GRAFANA_UPGRADES.read_text()
    assert srv.metrics_address == "127.0.0.1:8444"
    assert srv.trust[1:] == [{"fingerprint": dashboard.fingerprint(), "type": "metrics", "name": "thinny-metrics"}]
    assert oct(dashboard.KEY.stat().st_mode & 0o777) == "0o600"
    assert dashboard.KEY.read_text() == "private key"
    assert any(call.startswith("chown prometheus:prometheus") and call.endswith(".metrics.key.tmp") for call in srv.calls)
    assert oct(dashboard.PASSWORD.stat().st_mode & 0o777) == "0o600"
    assert dashboard.PASSWORD.read_text().startswith("GF_SECURITY_ADMIN_PASSWORD=")
    assert dashboard.dropin("grafana-server.service").read_text() == render.grafana_dropin(ON, str(dashboard.PASSWORD))
    assert dashboard.dropin("prometheus.service").read_text() == \
        render.prometheus_dropin(ON, str(dashboard.PROMETHEUS_CONFIG))
    assert srv.index("promtool check config") < srv.index("systemctl unmask") \
        < srv.index(f"systemctl restart {SERVICES} thinny-metrics.timer") \
        < srv.index(f"systemctl enable --now {SERVICES} thinny-metrics.timer")
    assert not srv.masked and srv.enabled == {*SERVICES.split(), "thinny-metrics.timer"}
    assert 'thinny_instance_disk_used_bytes{name="alice"} 1073741824' in dashboard.DISK_METRICS.read_text()
    assert host.dashboard_access() == (9200, "local")
    out = capsys.readouterr().out
    assert out.endswith("Dashboard: http://localhost:9200/ (user admin; its first password: sudo cat "
                        f"{dashboard.PASSWORD})\n  Open with: ssh -L 9200:127.0.0.1:9200 <you>@<server>\n")

    srv.calls.clear()
    password = dashboard.PASSWORD.read_text()
    dashboard.apply(ON, Registry.load())   # nothing new, so nothing restarted: Grafana takes 10 s to start
    assert not [call for call in srv.calls if call.startswith(
        ("apt-get", "systemctl mask", "systemctl restart", "openssl", "incus config set", "incus config trust add"))]
    assert dashboard.PASSWORD.read_text() == password and srv.prompts == ["Install the dashboard? [y/N] "]
    assert capsys.readouterr().out == ""


def test_the_address_comes_once_prometheus_and_grafana_answer(server, capsys):
    srv = server
    srv.answers = ["y"]
    srv.starting = {"http://127.0.0.1:9090/-/ready": ["000", "503"], "http://127.0.0.1:9200/api/health": ["000"] * 5}
    dashboard.apply(ON, Registry.load())
    assert [call for call in srv.calls if call.startswith("GET")] == \
        ["GET http://127.0.0.1:9090/-/ready"] * 3 + ["GET http://127.0.0.1:9200/api/health"] * 6
    assert srv.index(f"systemctl enable --now {SERVICES}") < srv.index("GET")
    assert srv.clock == 14   # seconds waited, before the address
    assert "Dashboard: http://localhost:9200/" in capsys.readouterr().out

    srv.starting = {"http://127.0.0.1:9300/api/health": ["000"] * 100}   # Grafana does not start
    with pytest.raises(CommandError, match=r"grafana-server does not answer at http://127.0.0.1:9300/api/health \(HTTP 000\) "
                                           r"after 120 s: journalctl -u grafana-server"):
        dashboard.apply(dataclasses.replace(ON, dashboard_port=9300), Registry.load())


def test_a_run_that_stopped_before_restarting_the_services_has_the_next_restart_them(server):
    srv = server
    srv.answers = ["y"]
    dashboard.apply(ON, Registry.load())
    srv.fails = {"promtool"}   # e.g. a bad Prometheus config, since fixed
    with pytest.raises(CommandError):
        dashboard.apply(dataclasses.replace(ON, dashboard_retention_days=60), Registry.load())
    srv.fails = set()
    srv.calls.clear()
    dashboard.apply(dataclasses.replace(ON, dashboard_retention_days=60), Registry.load())   # its files are written
    assert f"systemctl restart {SERVICES} thinny-metrics.timer" in srv.calls
    srv.calls.clear()
    dashboard.apply(dataclasses.replace(ON, dashboard_retention_days=60), Registry.load())
    assert not [call for call in srv.calls if call.startswith("systemctl restart")]


def test_a_new_port_or_access_moves_its_address(server, capsys):
    srv = server
    srv.answers = ["y"]
    dashboard.apply(dataclasses.replace(ON, dashboard_access="tailscale"), Registry.load())
    srv.served.add(9200)   # reconcile_access serves it
    lan = dataclasses.replace(ON, dashboard_access="lan", dashboard_port=9300)
    dashboard.apply(lan, Registry.load())
    assert "Environment=GF_SERVER_HTTP_PORT=9300\n" in dashboard.dropin("grafana-server.service").read_text()
    assert not srv.served and host.dashboard_access() == (9300, "lan")
    assert capsys.readouterr().out.endswith(
        "Dashboard: https://192.168.1.10:9300/ (user admin; its first password: sudo cat "
        f"{dashboard.PASSWORD})\n  Open it in a browser profile of its own: browsers send its sign-in to every "
        "Webapp at this address (see README, Dashboard).\n")


@pytest.mark.parametrize("instance, address, message", [
    (Instance("bob", 9200, "local"), "", "but bob uses one"),   # pending: its add goes on
    (Instance("bob", 9100, "local", state="active"), "", "but bob uses one"),
    (None, "[::]:8444", r"Incus serves its metrics at \[::\]:8444"),
])
def test_it_takes_no_port_or_metrics_address_that_is_not_free(server, instance, address, message):
    srv = server
    srv.answers, srv.metrics_address = ["y"], address
    reg = Registry.load()
    if instance:
        reg.instances[instance.name] = instance
    with pytest.raises(CommandError, match=message):
        dashboard.apply(ON, reg)
    assert srv.prompts == [] and files(srv) == []


def test_turning_it_off_takes_back_all_but_the_packages_password_and_history(server):
    srv = server
    srv.answers = ["y"]
    dashboard.apply(dataclasses.replace(ON, dashboard_access="tailscale"), Registry.load())
    srv.served.add(9200)
    dashboard.USAGE_METRICS.write_text(render.usage_metrics([("alice", "ChatGPT", "week", 0.5)]))
    srv.calls.clear()
    dashboard.apply(S, Registry.load())
    assert not srv.served
    assert srv.masked == set(SERVICES.split()) and f"systemctl mask --now {SERVICES}" in srv.calls
    assert srv.enabled == set()
    assert srv.metrics_address == "" and [entry["type"] for entry in srv.trust] == ["client"]
    assert files(srv) == ["etc/thinny/dashboard.env", "etc/thinny/metrics.crt", "etc/thinny/metrics.key",
                          "etc/thinny/prometheus.yml"]
    assert not [call for call in srv.calls if call.startswith("apt-get")]
    assert host.dashboard_access() is None

    srv.calls.clear()
    dashboard.apply(S, Registry.load())
    assert srv.calls == []

    srv.calls.clear()
    dashboard.apply(ON, Registry.load())   # on again: installed, so nothing to ask
    assert srv.metrics_address == "127.0.0.1:8444" and len(srv.trust) == 2
    assert srv.prompts == ["Install the dashboard? [y/N] "]
    assert not srv.masked and dashboard.GRAFANA_SOURCE.exists()
    assert not [call for call in srv.calls if call.startswith("apt-get")]


def test_off_leaves_a_metrics_address_thinny_did_not_set(server):
    srv = server
    srv.answers = ["y"]
    dashboard.apply(ON, Registry.load())
    srv.metrics_address = "10.0.0.5:8444"   # the admin's, since
    dashboard.apply(S, Registry.load())
    assert srv.metrics_address == "10.0.0.5:8444"


def test_the_timer_writes_the_disk_use_of_every_instance_with_a_container(server, monkeypatch):
    srv = server
    srv.containers = {"alice": 30 * 2**30, "bob": 10 * 2**30}
    reg = Registry({"alice": Instance("alice", 9120, "lan", state="active"),
                    "bob": Instance("bob", 9121, "local", state="disabled"),   # its disk stays
                    "carol": Instance("carol", 9122, "local")})   # its add stopped before its container
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    monkeypatch.setattr(cli, "load", lambda: pytest.fail("a broken config.toml must not stop it"))
    assert cli.main(["metrics"]) == 0
    assert dashboard.DISK_METRICS.read_text() == \
        render.disk_metrics([("alice", 2**30, 30 * 2**30), ("bob", 2**30, 10 * 2**30)])

    srv.fails.add("btrfs qgroup show")
    assert cli.main(["metrics"]) == 1   # not a 0: the last file stays, and grows old
    assert "bob" in dashboard.DISK_METRICS.read_text()


def usage(provider, *windows, reason=None):
    """`hermes usage --json` of `provider` with these (label, used percent) windows."""
    return json.dumps({"provider": provider, "source": "s", "title": "t", "plan": "Plus",
                       "fetched_at": "2026-11-19T15:00:00+00:00", "details": [], "unavailable_reason": reason,
                       "windows": [{"label": label, "used_percent": used, "resets_at": None, "detail": None}
                                   for label, used in windows]})


def asked(srv):
    """The instances whose Hermes was asked for its subscription usage."""
    return sorted(call.split()[2] for call in srv.calls if call.endswith("hermes usage --json"))


def test_the_timer_writes_each_running_instances_share_of_its_subscription_limits(server, monkeypatch):
    srv = server
    names = ["alice", "bob", "carol", "dave", "erin", "frank", "gina", "hank", "ivy", "jack"]
    reg = Registry({name: Instance(name, 9120 + i, "lan", state="active") for i, name in enumerate(names)})
    reg.get("ivy").state = "disabled"   # its container stays
    reg.get("jack").state = "pending"   # its add goes on
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    srv.containers = {name: 30 * 2**30 for name in names}
    srv.stopped = {"hank"}
    srv.settings = USAGE
    srv.usage = {
        "alice": usage("openai-codex", ("Session", 37.0), ("Weekly", 12.4)),
        "bob": usage(instances.CLAUDE_PROVIDER, ("Current session", 81), ("Opus week", 90), ("Current week", 64)),
        "carol": usage(instances.CLAUDE_PROVIDER, reason="no Claude subscription login found (run `claude`)"),
        # dave: Hermes got nothing (no login, or no answer)
        "erin": CommandError("hermes usage --json: timed out after 20 s"),
        "frank": "Traceback (most recent call last):",
        "gina": usage("openrouter", ("Session", 5)),   # no subscription of thinny's
        "ivy": usage("openai-codex", ("Session", 1)),
        "jack": usage("openai-codex", ("Session", 1)),
    }
    assert cli.main(["metrics"]) == 0
    assert dashboard.USAGE_METRICS.read_text() == render.usage_metrics([
        ("alice", "ChatGPT", "5h", 0.37), ("alice", "ChatGPT", "week", 0.124),
        ("bob", "Claude", "5h", 0.81), ("bob", "Claude", "week", 0.64)])
    assert asked(srv) == ["alice", "bob", "carol", "dave", "erin", "frank", "gina"]   # the provider: Hermes's
    assert "disk_used_bytes{name=\"hank\"}" in dashboard.DISK_METRICS.read_text()


def test_only_thinnys_providers_and_percentages_from_an_instances_hermes_count(server, monkeypatch):
    srv = server
    names = ["alice", "bob", "carol", "dave", "erin", "frank", "gina", "hank", "ivy"]
    reg = Registry({name: Instance(name, 9120 + i, "lan", state="active") for i, name in enumerate(names)})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    srv.containers = {name: 30 * 2**30 for name in names}
    srv.settings = USAGE
    # Its users control what their Hermes says.
    srv.usage = {
        "alice": usage("openai-codex", ("Session", True), ("Weekly", float("nan"))),
        "bob": usage("openai-codex", ("Session", 10**400), ("Weekly", "37")),
        "carol": usage("openai-codex", ("Session", 100.5), ("Weekly", -1)),
        "dave": json.dumps({"provider": ["openai-codex"], "windows": []}),
        "erin": json.dumps({"provider": "openai-codex", "windows": [["Session", 3]]}),
        "frank": json.dumps([1]),
        "gina": usage('openai-codex",name="bob', ("Session", 3)),
        "hank": usage("openai-codex", ("Session", 0), ("Weekly", 100)),
        "ivy": "[" * 10**5 + "]" * 10**5,   # too deep for Python's JSON
    }
    assert cli.main(["metrics"]) == 0
    assert dashboard.USAGE_METRICS.read_text() == render.usage_metrics([("hank", "ChatGPT", "5h", 0.0),
                                                                        ("hank", "ChatGPT", "week", 1.0)])


def test_it_asks_once_a_clock_hour_at_the_timers_first_run_in_it(server):
    srv = server
    srv.settings = USAGE
    srv.usage = {"alice": usage("openai-codex", ("Session", 37), ("Weekly", 12))}
    hours = []
    for minute in range(-10, 125, 5):   # every 5 minutes, from 10 minutes before the hour to 2 hours after it
        srv.now = HOUR + minute * 60 + 3
        srv.calls.clear()
        assert cli.main(["metrics"]) == 0
        if asked(srv):
            hours.append(minute)
    assert hours == [-10, 0, 60, 120]


def test_it_leaves_out_the_instance_whose_hermes_an_update_is_changing(server, monkeypatch):
    srv = server
    reg = Registry({name: Instance(name, 9120 + i, "lan", state="active") for i, name in enumerate(["alice", "bob"])})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    srv.containers = {"alice": 30 * 2**30, "bob": 30 * 2**30}
    srv.settings = USAGE
    srv.usage = {name: usage("openai-codex", ("Session", 37), ("Weekly", 12)) for name in ("alice", "bob")}
    update.UNFINISHED.write_text("v0.23.2 abc123 bob\n")
    srv.now = HOUR
    assert cli.main(["metrics"]) == 0
    assert asked(srv) == ["alice"]
    assert dashboard.USAGE_METRICS.read_text() == render.usage_metrics([("alice", "ChatGPT", "5h", 0.37),
                                                                        ("alice", "ChatGPT", "week", 0.12)])
    update.UNFINISHED.write_text("v0.23.2 abc123\n")   # not on an instance yet
    srv.now = HOUR + 3600
    srv.calls.clear()
    cli.main(["metrics"])
    assert asked(srv) == ["alice", "bob"]


def test_a_round_keeps_only_what_it_got_and_freshens_the_file_also_when_it_stays(server, monkeypatch):
    srv = server
    reg = Registry({name: Instance(name, 9120 + i, "lan", state="active") for i, name in enumerate(["alice", "bob"])})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    srv.containers = {"alice": 30 * 2**30, "bob": 30 * 2**30}
    srv.settings = USAGE
    srv.usage = {name: usage("openai-codex", ("Session", 37), ("Weekly", 12)) for name in ("alice", "bob")}
    srv.now = HOUR
    cli.main(["metrics"])
    assert 'name="bob"' in dashboard.USAGE_METRICS.read_text()
    srv.usage["bob"] = ""   # e.g. the provider did not answer
    srv.now = HOUR + 3600
    cli.main(["metrics"])
    alice = render.usage_metrics([("alice", "ChatGPT", "5h", 0.37), ("alice", "ChatGPT", "week", 0.12)])
    assert dashboard.USAGE_METRICS.read_text() == alice
    srv.now = HOUR + 7200
    cli.main(["metrics"])
    assert dashboard.USAGE_METRICS.read_text() == alice and dashboard.USAGE_METRICS.stat().st_mtime == HOUR + 7200


@pytest.mark.parametrize("off", [ON, dataclasses.replace(USAGE, dashboard_enabled=False)])
def test_off_it_asks_nothing_and_takes_its_file_away_and_on_again_it_asks_at_once(server, off):
    srv = server
    srv.settings = USAGE
    srv.usage = {"alice": usage("openai-codex", ("Session", 37), ("Weekly", 12))}
    srv.now = HOUR
    cli.main(["metrics"])
    srv.settings = off
    srv.calls.clear()
    assert cli.main(["metrics"]) == 0
    assert not dashboard.USAGE_METRICS.exists() and not asked(srv)
    assert dashboard.DISK_METRICS.exists()
    srv.settings = USAGE
    srv.now = HOUR + 600   # in the same hour
    cli.main(["metrics"])
    assert asked(srv) == ["alice"] and dashboard.USAGE_METRICS.exists()


def test_a_broken_config_toml_stops_only_the_subscription_usage(server, monkeypatch):
    def broken():
        raise SettingsError("/etc/thinny/config.toml: Expected '=' after a key (at line 3, column 5)")
    monkeypatch.setattr(dashboard, "load", broken)
    assert cli.main(["metrics"]) == 1
    assert 'disk_used_bytes{name="alice"}' in dashboard.DISK_METRICS.read_text()


def test_the_update_renews_grafanas_key_only_while_its_repository_is_in_use(server, monkeypatch, capsys):
    dashboard.renew_key()
    assert not dashboard.GRAFANA_KEY.exists()
    dashboard.GRAFANA_SOURCE.parent.mkdir(parents=True)
    dashboard.GRAFANA_SOURCE.write_text(render.grafana_source(str(dashboard.GRAFANA_KEY)))
    dashboard.renew_key()
    assert dashboard.GRAFANA_KEY.read_text() == KEY

    def offline(url, timeout):
        raise dashboard.urllib.error.URLError("Temporary failure in name resolution")
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", offline)
    dashboard.renew_key()   # the update goes on
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"<html>"))
    dashboard.renew_key()
    assert dashboard.GRAFANA_KEY.read_text() == KEY
    err = capsys.readouterr().err
    assert "Grafana's apt key not renewed: <urlopen error Temporary failure in name resolution>" in err
    assert "is not an OpenPGP key" in err


def test_the_dashboard_reads_the_provisioned_prometheus_and_thinnys_disk_file():
    board = json.loads((host.FILES / "grafana/thinny.json").read_text())
    assert render.GRAFANA_SETTINGS["USERS_HOME_PAGE"] == f"/d/{board['uid']}"
    panels = [panel for panel in board["panels"] if panel["type"] != "row"]
    assert {panel["datasource"]["uid"] for panel in panels} | \
        {target["datasource"]["uid"] for panel in panels for target in panel["targets"]} == \
        {render.GRAFANA_DATASOURCE_UID}
    assert f"uid: {render.GRAFANA_DATASOURCE_UID}\n" in render.GRAFANA_DATASOURCE
    instances = board["templating"]["list"][0]["query"]   # the instances to choose from: thinny's
    assert instances == "label_values(thinny_instance_disk_quota_bytes, name)"
    assert 'thinny_instance_disk_quota_bytes{name="alice"}' in render.disk_metrics([("alice", 1, 2)])


def test_the_dashboard_shows_the_subscription_usage_only_while_thinnys_file_of_it_is_fresh():
    board = json.loads((host.FILES / "grafana/thinny.json").read_text())
    fresh = f' and on() (time() - node_textfile_mtime_seconds{{file="{dashboard.USAGE_METRICS}"}} < 7200)'
    usage = {panel["title"]: panel for panel in board["panels"]
             if any("thinny_instance_subscription_used_ratio{" in target["expr"] for target in panel.get("targets", []))}
    assert list(usage) == ["Weekly subscription limit used", "Subscription limits"]
    exprs = [target["expr"] for panel in usage.values() for target in panel["targets"]]
    assert all(expr.endswith(fresh) for expr in exprs)
    assert {window for expr in exprs for window in re.findall(r'window="(\w+)"', expr)} == set(dashboard.USAGE_WINDOWS)
    assert 'thinny_instance_subscription_used_ratio{name="alice",provider="ChatGPT",window="week"} 0.5' in \
        render.usage_metrics([("alice", "ChatGPT", "week", 0.5)])
    # the fleet's in the row of the 10 with the most, the chosen instance's in its row
    rows = {panel["title"]: panel["gridPos"]["y"] for panel in board["panels"] if panel["type"] == "row"}
    assert rows["Instances: the 10 with the most"] < usage["Weekly subscription limit used"]["gridPos"]["y"] \
        < rows["Instance $instance"] < usage["Subscription limits"]["gridPos"]["y"]
    assert all('{name="$instance",' in target["expr"] for target in usage["Subscription limits"]["targets"])
    cells = [(x, y) for panel in board["panels"] for grid in [panel["gridPos"]]
             for x in range(grid["x"], grid["x"] + grid["w"]) for y in range(grid["y"], grid["y"] + grid["h"])]
    assert len(cells) == len(set(cells))   # no panel covers another
    assert len({panel["id"] for panel in board["panels"]}) == len(board["panels"])


def test_it_waits_for_tailscale_rather_than_forget_what_tailscale_serves(server, monkeypatch):
    srv = server
    srv.answers = ["y"]
    dashboard.apply(dataclasses.replace(ON, dashboard_access="tailscale"), Registry.load())
    srv.served.add(9200)
    srv.tailscale = None   # Tailscale keeps serving the port once it is back
    for s in (S, ON):   # off, or local
        with pytest.raises(CommandError, match="Tailscale is not running, so thinny cannot stop serving the "
                                               "dashboard at port 9200"):
            dashboard.apply(s, Registry.load())
        assert host.dashboard_access() == (9200, "tailscale")
    srv.tailscale = {"BackendState": "Running"}

    def unknown():
        raise CommandError("tailscale serve status --json -> exit 1")
    monkeypatch.setattr(host, "tailscale_served_ports", unknown)
    with pytest.raises(CommandError, match="tailscale serve status"):
        dashboard.apply(S, Registry.load())
    assert host.dashboard_access() == (9200, "tailscale")
    monkeypatch.setattr(host, "tailscale_served_ports", lambda: set(srv.served))
    dashboard.apply(S, Registry.load())
    assert not srv.served and host.dashboard_access() is None


def test_turning_it_off_takes_back_a_turn_on_that_stopped_halfway(server):
    srv = server
    srv.answers, srv.fails = ["y"], {"openssl"}
    with pytest.raises(CommandError):
        dashboard.apply(ON, Registry.load())
    assert not dashboard.CERT.exists() and srv.masked
    srv.fails = set()
    dashboard.apply(S, Registry.load())
    assert host.dashboard_access() is None and not dashboard.GRAFANA_SOURCE.exists()
    assert srv.masked == set(SERVICES.split()) and srv.metrics_address == ""


def test_doctor_sees_the_disk_file_fresh_also_when_its_numbers_stay(server):
    dashboard.write_disk_metrics()
    old = dashboard.DISK_METRICS.stat().st_mtime - 600
    os.utime(dashboard.DISK_METRICS, (old, old))
    dashboard.write_disk_metrics()
    assert dashboard.DISK_METRICS.stat().st_mtime > old + 500
