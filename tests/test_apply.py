import dataclasses
import io
import subprocess

import pytest
import yaml
from test_instances import LOCKED, incus  # noqa: F401 - incus: the fake Incus fixture

from thinny import apply, dashboard, host, instances, render, system, trace
from thinny.registry import Instance, Registry
from thinny.settings import Settings
from thinny.system import CommandError

S = Settings(model="m1", allowed_models=("m1",), access_default="local")


@pytest.fixture
def policies(monkeypatch):
    """Every instance has a container; records which instances' policies are written."""
    written = []
    monkeypatch.setattr(instances, "container_state", lambda name: "RUNNING")
    monkeypatch.setattr(instances, "ensure_device", lambda *a: None)
    monkeypatch.setattr(instances, "devices", lambda name: {})
    monkeypatch.setattr(host, "write_policy", lambda s, name: written.append(name))
    return written


def test_a_policy_change_restarts_every_instance_even_after_a_failed_apply(tmp_path, monkeypatch, policies):
    reg = Registry({n: Instance(n, 9120 + i, "local", state="active") for i, n in enumerate(["alice", "bob"])})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    monkeypatch.setattr(instances, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    monkeypatch.setattr(host, "policy_current", lambda s, name: name in policies)
    for step in ("install_units", "ensure_profile"):
        monkeypatch.setattr(host, step, lambda *a: None)
    monkeypatch.setattr(instances, "ensure_trace", lambda name: None)
    monkeypatch.setattr(instances, "url_for", lambda s, inst: inst.url)
    monkeypatch.setattr(instances, "configure_webapp", lambda s, inst: False)
    restarted = []
    monkeypatch.setattr(instances, "_gateway_up", lambda s, inst: restarted.append(inst.name))

    monkeypatch.setattr(host, "firewall", lambda s: None)

    def tailscale_down(s, reg):
        raise CommandError("tailscale serve -> exit 1")
    monkeypatch.setattr(host, "reconcile_access", tailscale_down)
    with pytest.raises(CommandError):
        apply.apply(S)          # the new policy is written, but no instance was restarted
    assert policies == ["alice", "bob"] and restarted == []

    monkeypatch.setattr(host, "reconcile_access", lambda s, reg: None)
    apply.apply(S)              # nothing changes any more, yet the restarts are still owed
    assert restarted == ["alice", "bob"]


@pytest.mark.parametrize("model_lock", [True, False])
def test_turning_the_model_lock_on_first_removes_the_model_instances_started_with(tmp_path, monkeypatch, policies,
                                                                                 model_lock):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active"),
                    "bob": Instance("bob", 9121, "local", state="pending")})   # an add that stopped halfway
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    monkeypatch.setattr(instances, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    for step in ("firewall", "install_units", "ensure_profile", "reconcile_access"):
        monkeypatch.setattr(host, step, lambda *a: None)
    monkeypatch.setattr(instances, "ensure_trace", lambda name: None)
    monkeypatch.setattr(instances, "url_for", lambda s, inst: inst.url)
    monkeypatch.setattr(instances, "configure_webapp", lambda s, inst: False)
    monkeypatch.setattr(instances, "_gateway_up", lambda s, inst: None)
    monkeypatch.setattr(host, "policy_current", lambda s, name: False)
    done = policies
    monkeypatch.setattr(instances, "hermes", lambda name, args, **kw: done.append(f"{name}: hermes {' '.join(args)}"))

    apply.apply(dataclasses.replace(S, model_lock=model_lock))
    # Hermes would drop them from the instance's config once pinned, and can leave it unreadable.
    keys = ("model.provider", "model.default", "agent.reasoning_effort") if model_lock else ()
    assert done == [step for name in ("alice", "bob")
                    for step in [f"{name}: hermes config unset {key}" for key in keys] + [name]]


@pytest.fixture
def server(incus, monkeypatch):
    """apply on the fake Incus with the registry it returns; records the restarts."""
    reg = Registry()
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    for step in ("firewall", "install_units", "ensure_profile"):
        monkeypatch.setattr(host, step, lambda *a: None)
    monkeypatch.setattr(instances, "url_for", lambda s, inst: inst.url)
    monkeypatch.setattr(instances, "configure_webapp", lambda s, inst: False)
    restarted = []
    monkeypatch.setattr(instances, "_gateway_up", lambda s, inst: restarted.append(inst.name))
    return reg, restarted


def test_apply_moves_every_container_to_a_policy_of_its_own(server, incus):
    reg, restarted = server
    # alice and carol from before, on the policy all instances shared; bob's add stopped before his container
    shared = {"type": "disk", "source": "/etc/hermes", "path": "/etc/hermes", "readonly": "true", "shift": "true"}
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active"),
                         bob=Instance("bob", 9121, "local", state="pending"),
                         carol=Instance("carol", 9122, "local", state="disabled"))
    for name in ("alice", "carol"):
        incus.containers[name] = {"state": "RUNNING", "devices": {"policy": dict(shared)}}

    apply.apply(S)
    assert incus.containers["alice"]["devices"]["policy"] == \
        shared | {"source": str(host.POLICIES / "alice"), "shift": "false"}
    assert (host.POLICIES / "alice/config.yaml").read_text() == render.managed_config(S)
    assert restarted == ["alice"]
    assert incus.containers["carol"]["devices"]["policy"] == shared and sorted(host.POLICIES.iterdir()) == \
        [host.POLICIES / "alice"]   # bob's add writes his

    apply.apply(S)
    assert restarted == ["alice"]


def test_apply_takes_the_shift_off_the_disks_containers_have(server, incus):
    # Incus does not lock a disk added to a running container read-only: with shift, the container's root
    # could remount it and write into the host's folder as the host's root.
    reg, _ = server
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active"),
                         bob=Instance("bob", 9121, "local", state="pending"),     # stopped after its model guard
                         carol=Instance("carol", 9122, "local", state="pending"),  # stopped before its Hermes
                         dave=Instance("dave", 9123, "local", state="pending"))    # a move stopped after its import
    for name in reg.instances:
        guard = f"/home/{name}/.hermes/hermes-agent/plugins/model-guard"
        incus.containers[name] = {"state": "STOPPED" if name == "dave" else "RUNNING", "devices": {
            "policy": {"type": "disk", "source": str(host.POLICIES / name), "path": "/etc/hermes", "readonly": "true",
                       "shift": "true"},
            "model-guard": {"type": "disk", "source": str(host.FILES / "model-guard"), "path": guard,
                            "readonly": "true", "shift": "true"}}}
    del incus.containers["carol"]["devices"]["model-guard"]
    apply.apply(S)
    shifts = {name: {device: props["shift"] for device, props in c["devices"].items()}
              for name, c in incus.containers.items()}
    assert shifts == {"alice": {"policy": "false", "model-guard": "false", "trace": "false"},   # active: its trace
                      "bob": {"policy": "false", "model-guard": "false"},
                      "carol": {"policy": "false"},
                      "dave": {"policy": "false", "model-guard": "false"}}


def test_apply_keeps_the_locks_instances_have_on_their_own(server, incus):
    reg, restarted = server
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active", lock=["m2"], lock_provider="nous"),
                         bob=Instance("bob", 9121, "local", state="active", lock=[]),
                         carol=Instance("carol", 9122, "local", state="active"))
    for name in reg.instances:
        incus.containers[name] = {"state": "RUNNING", "devices": {}}
    apply.apply(LOCKED)
    assert host.policy_current(dataclasses.replace(LOCKED, model_provider="nous", model="m2", allowed_models=("m2",)),
                               "alice")
    assert host.policy_current(S, "bob") and host.policy_current(LOCKED, "carol")


LOCKS_MODEL = {"model.provider": "openai-codex", "agent.reasoning_effort": "high", "model.default": "m1"}


def test_turning_the_model_lock_off_leaves_instances_on_the_locks_model(server, incus):
    reg, restarted = server
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active"),
                         bob=Instance("bob", 9121, "local", state="active", lock=["m2"], lock_provider="nous"),
                         carol=Instance("carol", 9122, "local", state="active", lock=[]),
                         dave=Instance("dave", 9123, "local", state="active"))
    for name in reg.instances:
        incus.containers[name] = {"state": "RUNNING", "devices": {}}
    apply.apply(LOCKED)
    incus.containers["carol"]["hermes_config"] = {"model.provider": "nous", "model.default": "hermes-4"}
    incus.containers["dave"]["hermes_config"] = {"model.default": "gpt-5.5"}   # a model of its own all along
    restarted.clear()

    # The lock's model comes from its policy: config.toml may have another, or none, without the lock.
    unlocked = dataclasses.replace(S, model="", reasoning_effort="low")
    apply.apply(unlocked)
    assert incus.containers["alice"]["hermes_config"] == LOCKS_MODEL
    assert "hermes_config" not in incus.containers["bob"]   # still on its own lock
    assert incus.containers["carol"]["hermes_config"] == {"model.provider": "nous", "model.default": "hermes-4"}
    assert incus.containers["dave"]["hermes_config"] == {"model.default": "gpt-5.5"}
    assert restarted == ["alice", "bob", "dave"]   # bob: the server's effort changed

    incus.containers["alice"]["hermes_config"]["model.default"] = "gpt-5.5"   # its users chose another
    apply.apply(unlocked)
    assert incus.containers["alice"]["hermes_config"]["model.default"] == "gpt-5.5"
    assert restarted == ["alice", "bob", "dave"]


def test_an_apply_that_failed_after_unlocking_an_instance_still_gives_it_the_locks_model(server, incus, monkeypatch):
    reg, restarted = server
    reg.instances["alice"] = Instance("alice", 9120, "local", state="active")
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}}
    apply.apply(LOCKED)
    set_config = instances.set_config

    def failing(s, name, values):
        raise CommandError("hermes config set -> exit 1")
    monkeypatch.setattr(instances, "set_config", failing)
    with pytest.raises(CommandError):
        apply.apply(S)
    assert host.policy_current(S, "alice") and not incus.containers["alice"].get("hermes_config")

    monkeypatch.setattr(instances, "set_config", set_config)
    apply.apply(S)
    assert incus.containers["alice"]["hermes_config"] == LOCKS_MODEL
    assert restarted == ["alice", "alice"]


def test_running_instances_get_their_trace(server, incus):
    reg, _ = server
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active"),
                         bob=Instance("bob", 9121, "local", state="disabled"))
    for name, state in (("alice", "RUNNING"), ("bob", "STOPPED")):
        incus.containers[name] = {"state": state, "devices": {}}

    apply.apply(S)
    assert incus.containers["alice"]["devices"]["trace"] == {
        "type": "disk", "source": str(trace.DIR / "alice"), "path": "/var/log/thinny", "readonly": "true",
        "shift": "false", "required": "false"}
    assert (trace.DIR / "alice").is_dir()
    assert incus.containers["bob"]["devices"] == {}


def test_mailboxes_from_before_get_the_email_client(server, incus):
    reg, _ = server
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active", mailbox="alice@example.org"),
                         bob=Instance("bob", 9121, "local", state="active"))
    for name in reg.instances:
        incus.containers[name] = {"state": "RUNNING", "devices": {}}

    apply.apply(S)
    assert incus.containers["alice"]["himalaya"] == instances.HIMALAYA
    assert '[accounts."alice@example.org"]' in incus.containers["alice"]["files"]["/home/alice/.config/himalaya/config.toml"]
    assert "himalaya" not in incus.containers["bob"] and "files" not in incus.containers["bob"]


def test_agents_of_mailboxes_from_before_learn_how_to_send_mail_after_their_groups_own_hint(server, incus):
    reg, restarted = server
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active", group="sales",
                                        mailbox="alice@example.org"),
                         bob=Instance("bob", 9121, "local", state="active", group="sales"))
    for name in reg.instances:
        incus.containers[name] = {"state": "RUNNING", "devices": {}, "hermes_config": {"agent.environment_hint": "x"}}
    sales = {"agent": {"environment_hint": "Office hours: 9-17."}}
    s = dataclasses.replace(S, groups={"sales": sales})

    apply.apply(s)
    hints = {name: yaml.safe_load((host.POLICIES / name / "config.yaml").read_text())["agent"]["environment_hint"]
             for name in reg.instances}
    assert hints == {"alice": "Office hours: 9-17.\n\n" + render.mail_hint("alice@example.org", instances.HIMALAYA_BIN),
                     "bob": "Office hours: 9-17."}
    assert incus.containers["alice"]["hermes_config"] == {}   # pinned now
    assert restarted == ["alice", "bob"]
    assert sales == {"agent": {"environment_hint": "Office hours: 9-17."}}   # the group's own stays as it is


def test_members_of_a_group_get_its_settings_after_losing_their_own(server, incus):
    reg, restarted = server
    reg.instances.update(alice=Instance("alice", 9120, "local", state="active", group="sales"),
                         bob=Instance("bob", 9121, "local", state="active"))
    for name in reg.instances:
        incus.containers[name] = {"state": "RUNNING", "devices": {},
                                  "hermes_config": {"display.language": "en", "display.skin": "mono"}}
    sales = {"display": {"language": "cs"}, "mcp_servers": {"github": {"command": "npx"}}}
    s = dataclasses.replace(S, groups={"sales": sales})
    apply.apply(s)
    assert host.policy_current(dataclasses.replace(s, hermes=sales), "alice") and host.policy_current(s, "bob")
    # Hermes refuses to unset them once pinned, and drops them from a config it saves anyway.
    assert incus.containers["alice"]["hermes_config"] == {"display.skin": "mono"}
    assert incus.containers["bob"]["hermes_config"] == {"display.language": "en", "display.skin": "mono"}
    assert incus.ran("hermes config unset mcp_servers.github.command")
    assert restarted == ["alice", "bob"]


def test_a_member_of_a_missing_group_stops_apply_before_any_change(server, incus, monkeypatch):
    reg, restarted = server
    reg.instances["alice"] = Instance("alice", 9120, "local", state="pending", group="sales")
    reg.instances["bob"] = Instance("bob", 9121, "local", state="disabled", group="support")   # left alone
    changed = []
    monkeypatch.setattr(host, "firewall", lambda s: changed.append("firewall"))
    with pytest.raises(CommandError, match="alice is in the group sales, but .*groups/sales.toml is missing"):
        apply.apply(dataclasses.replace(S, groups={"support": {}}))
    assert changed == [] and not host.POLICIES.exists()


def test_without_a_terminal_apply_does_all_but_install_the_dashboard(server, incus, monkeypatch, capsys):
    reg, restarted = server
    reg.instances["alice"] = Instance("alice", 9120, "local", state="active")
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}}
    monkeypatch.setattr(dashboard, "run", incus.run)   # Incus's metrics are off, no package installed
    monkeypatch.setattr(dashboard.sys, "stdin", io.StringIO())   # e.g. the nightly update's install.sh
    apply.apply(dataclasses.replace(S, dashboard_enabled=True))
    assert "Run on a terminal: thinny config apply" in capsys.readouterr().err
    assert not incus.ran("apt-get") and not incus.ran("incus config set")
    assert host.policy_current(S, "alice") and restarted == ["alice"]


@pytest.fixture
def bare_setup(monkeypatch):
    """setup without Incus, apply, instances or ufw."""
    monkeypatch.setattr(host, "init_incus", lambda: None)
    monkeypatch.setattr(apply, "apply", lambda s: None)
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: Registry()))
    monkeypatch.setattr(host.shutil, "which", lambda cmd: None)
    monkeypatch.delenv("SUDO_USER", raising=False)


@pytest.mark.parametrize("tunnel_port, hint", [(0, False), (8080, True)])
def test_setup_ends_with_where_the_tunnel_goes(bare_setup, capsys, tunnel_port, hint):
    apply.setup(dataclasses.replace(S, tunnel_port=tunnel_port))
    assert ("Point your tunnel at http://127.0.0.1:8080" in capsys.readouterr().out) == hint


@pytest.fixture
def ufw(bare_setup, tmp_path, monkeypatch):
    """ufw on a server with 192.168.1.191/24 and 10.0.0.5/32: the test writes its ufw.conf and the rules
    `ufw show added` lists."""
    monkeypatch.setattr(host.shutil, "which", lambda cmd: f"/usr/sbin/{cmd}")
    monkeypatch.setattr(host, "UFW_CONF", tmp_path / "ufw.conf")
    rules = []

    def run(cmd, **kw):
        if cmd[:5] == ["ip", "-o", "addr", "show", "to"]:
            address = {"192.168.1.191": "192.168.1.191/24", "10.0.0.5": "10.0.0.5/32"}.get(cmd[5])
            return subprocess.CompletedProcess(cmd, 0, f"2: eth0    inet {address} scope global eth0\n" if address else "", "")
        if cmd != ["ufw", "show", "added"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        added = "\n".join(["Added user rules (see 'ufw status' for running firewall):", *(rules or ["(None)"])])
        return subprocess.CompletedProcess(cmd, 0, added + "\n", "")
    monkeypatch.setattr(host, "run", run)
    return rules


LAN = dataclasses.replace(S, access_default="lan", lan_address="192.168.1.191")
OFFICE = ["ufw allow from 192.168.1.0/24 to any port 9120:9199 proto tcp",
          "ufw allow from 192.168.1.0/24 to any port 80 proto tcp"]


@pytest.mark.parametrize("enabled, rules, hint", [
    ("no", [], False),
    ("yes", [], True),
    ("yes", ["ufw allow 22/tcp"], True),
    ("yes", ["ufw allow 22/tcp", "ufw route allow in on thinny-+"], False),
    ("yes", ["ufw route allow in on thinny-+ comment 'Incus containers'"], False),
])
def test_setup_tells_the_admin_when_ufw_lacks_the_containers_forward_rule(ufw, capsys, enabled, rules, hint):
    host.UFW_CONF.write_text(f"# Set to yes to start on boot.\nENABLED={enabled}\n\nLOGLEVEL=low\n")
    ufw.extend(rules)
    apply.setup(S)
    out = capsys.readouterr().out
    assert ("ufw route allow in on thinny-+" in out) == hint
    assert out.endswith("Ready. Add instances with: thinny instance add <name>\n")


def test_setup_skips_the_ufw_hints_while_ufw_waits_for_its_lock(ufw, capsys, monkeypatch):
    host.UFW_CONF.write_text("ENABLED=yes\n")
    answer = host.run

    def waiting(cmd, **kw):   # an admin's `ufw delete` holds ufw's lock while it asks to confirm
        if cmd[0] != "ufw":
            return answer(cmd, **kw)
        raise CommandError(f"{' '.join(cmd)}: timed out after 30 s")
    monkeypatch.setattr(host, "run", waiting)
    apply.setup(LAN)
    assert capsys.readouterr().out == "Ready. Add instances with: thinny instance add <name>\n"


@pytest.mark.parametrize("sudo_user, command", [(None, "ufw"), ("admin", "sudo ufw")])
def test_the_ufw_hint_has_sudo_when_the_admin_used_it(ufw, capsys, monkeypatch, sudo_user, command):
    host.UFW_CONF.write_text("ENABLED=yes\n")
    if sudo_user:
        monkeypatch.setenv("SUDO_USER", sudo_user)
    apply.setup(S)
    assert f"(see README, Prerequisites). Run: {command} route allow in on thinny-+\n" in capsys.readouterr().out


@pytest.mark.parametrize("enabled, rules, hint", [
    ("no", [], []),
    ("yes", [], OFFICE),
    ("yes", [OFFICE[1]], OFFICE[:1]),
    ("yes", [OFFICE[0], f"{OFFICE[1]} comment 'root certificate'"], []),
])
def test_setup_tells_the_admin_when_ufw_keeps_office_pcs_out(ufw, capsys, enabled, rules, hint):
    host.UFW_CONF.write_text(f"ENABLED={enabled}\n")
    ufw.extend([host.UFW_FORWARD, *rules])
    apply.setup(LAN)
    out = capsys.readouterr().out
    assert [rule for rule in OFFICE if f"\n  {rule}\n" in out] == hint
    assert out.endswith("Ready. Add instances with: thinny instance add <name>\n")


@pytest.mark.parametrize("default, instance, hint", [
    ("lan", None, True),
    ("tailscale", Instance("alice", 9120, "lan", state="active"), True),
    ("tailscale", Instance("alice", 9120, "lan", state="disabled"), False),
    ("tailscale", Instance("alice", 9120, "tailscale", state="active"), False),
])
def test_the_office_hint_comes_only_with_lan_access_in_use(ufw, capsys, monkeypatch, default, instance, hint):
    host.UFW_CONF.write_text("ENABLED=yes\n")
    ufw.append(host.UFW_FORWARD)
    reg = Registry({instance.name: instance} if instance else {})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    apply.setup(dataclasses.replace(LAN, access_default=default))
    assert (OFFICE[0] in capsys.readouterr().out) == hint


def test_the_office_hint_has_the_configured_ports_and_sudo(ufw, capsys, monkeypatch):
    host.UFW_CONF.write_text("ENABLED=yes\n")
    ufw.append(host.UFW_FORWARD)
    monkeypatch.setenv("SUDO_USER", "admin")
    apply.setup(dataclasses.replace(LAN, port_range=(9200, 9249)))
    assert capsys.readouterr().out == (
        "ufw blocks office PCs, so they cannot open the Webapps or get the root certificate (see README, "
        "Prerequisites). Run:\n"
        "  sudo ufw allow from 192.168.1.0/24 to any port 9200:9249 proto tcp\n"
        "  sudo ufw allow from 192.168.1.0/24 to any port 80 proto tcp\n"
        "Ready. Add instances with: thinny instance add <name>\n")


def test_the_office_hint_opens_the_dashboard_on_the_lan_too(ufw, capsys):
    host.UFW_CONF.write_text("ENABLED=yes\n")
    ufw.append(host.UFW_FORWARD)
    host.DASHBOARD_STATE.write_text("9200 lan\n")
    apply.setup(dataclasses.replace(LAN, access_default="tailscale"))   # no instance on the LAN
    assert "\n  ufw allow from 192.168.1.0/24 to any port 9200 proto tcp\n" in capsys.readouterr().out


# On no interface (Caddy cannot bind it either), or on one with no network around it
@pytest.mark.parametrize("lan_address", ["10.0.0.9", "10.0.0.5"])
def test_setup_names_no_office_network_it_cannot_find(ufw, capsys, lan_address):
    host.UFW_CONF.write_text("ENABLED=yes\n")
    ufw.append(host.UFW_FORWARD)
    apply.setup(dataclasses.replace(LAN, lan_address=lan_address))
    assert capsys.readouterr().out == "Ready. Add instances with: thinny instance add <name>\n"
