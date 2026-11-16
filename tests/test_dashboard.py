import base64
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import types

import pytest

from thinny import cli, dashboard, host, instances, render, system
from thinny.registry import Instance, Registry
from thinny.settings import Settings
from thinny.system import CommandError

S = Settings(model="m1", allowed_models=("m1",), access_default="local", lan_address="192.168.1.10")
ON = dataclasses.replace(S, dashboard_enabled=True)
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
        self.answers = []
        self.prompts = []
        self.fails = set()   # commands that fail, by their start

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
            return json.dumps([{"name": name, "state": {"disk": {"root": {"total": quota}}}}
                               for name, quota in self.containers.items()]), 0
        elif cmd[:3] == ["btrfs", "qgroup", "show"]:
            return f"Qgroupid Referenced Exclusive Path\n0/257 {2**30} {2**29} {cmd[-1]}\n", 0
        elif cmd[:2] == ["systemctl", "mask"]:
            self.masked |= {unit for unit in cmd[2:] if unit != "--now"}
            self.enabled -= self.masked
        elif cmd[:2] == ["systemctl", "unmask"]:
            self.masked -= set(cmd[2:])
        elif cmd[:2] == ["systemctl", "enable"]:
            assert not set(cmd[2:]) & self.masked
            self.enabled |= set(cmd[2:])
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
                       ("DISK_METRICS", tmp_path / "var/lib/prometheus/node-exporter/thinny.prom")):
        monkeypatch.setattr(dashboard, name, path)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A server with alice, Tailscale running and 513 GB free; the admin at a terminal."""
    srv = Server()
    for module in (dashboard, instances):
        monkeypatch.setattr(module, "run", srv.run)
    dashboard_files(tmp_path, monkeypatch)
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
        "  - uses about 0.5 GB of RAM (estimate)\n"
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
    assert srv.index("promtool check config") < srv.index("systemctl unmask") < srv.index("systemctl enable") \
        < srv.index(f"systemctl restart {SERVICES} thinny-metrics.timer")
    assert not srv.masked and srv.enabled == {*SERVICES.split(), "thinny-metrics.timer"}
    assert 'thinny_instance_disk_used_bytes{name="alice"} 1073741824' in dashboard.DISK_METRICS.read_text()
    assert host.dashboard_access() == (9200, "local")
    out = capsys.readouterr().out
    assert out.endswith("Dashboard: http://localhost:9200/ (user admin; its first password: sudo cat "
                        f"{dashboard.PASSWORD})\n  Open with: ssh -L 9200:127.0.0.1:9200 <you>@<server>\n")

    srv.calls.clear()
    password = dashboard.PASSWORD.read_text()
    dashboard.apply(ON, Registry.load())   # nothing new
    assert not [call for call in srv.calls if call.startswith(
        ("apt-get", "systemctl mask", "openssl", "incus config set", "incus config trust add"))]
    # restarted all the same: a run that failed before restarting them left them on their old settings
    assert f"systemctl restart {SERVICES} thinny-metrics.timer" in srv.calls
    assert dashboard.PASSWORD.read_text() == password and srv.prompts == ["Install the dashboard? [y/N] "]
    assert capsys.readouterr().out == ""


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
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "load", lambda: pytest.fail("a broken config.toml must not stop it"))
    assert cli.main(["metrics"]) == 0
    assert dashboard.DISK_METRICS.read_text() == \
        render.disk_metrics([("alice", 2**30, 30 * 2**30), ("bob", 2**30, 10 * 2**30)])

    srv.fails.add("btrfs qgroup show")
    assert cli.main(["metrics"]) == 1   # not a 0: the last file stays, and grows old
    assert "bob" in dashboard.DISK_METRICS.read_text()


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
