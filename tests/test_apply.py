import dataclasses

import pytest
from test_instances import LOCKED, incus  # noqa: F401 - incus: the fake Incus fixture

from thinny import apply, host, instances, render, system, trace
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


@pytest.mark.parametrize("tunnel_port, hint", [(0, False), (8080, True)])
def test_setup_ends_with_where_the_tunnel_goes(monkeypatch, capsys, tunnel_port, hint):
    monkeypatch.setattr(host, "init_incus", lambda: None)
    monkeypatch.setattr(apply, "apply", lambda s: None)
    apply.setup(dataclasses.replace(S, tunnel_port=tunnel_port))
    assert ("Point your tunnel at http://127.0.0.1:8080" in capsys.readouterr().out) == hint
