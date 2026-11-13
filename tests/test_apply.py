import dataclasses

import pytest

from thinny import apply, host, people, system
from thinny.registry import Person, Registry
from thinny.settings import Settings
from thinny.system import CommandError

S = Settings(model="m1", allowed_models=("m1",), access_default="local")


def test_a_policy_change_restarts_everyone_even_after_a_failed_apply(tmp_path, monkeypatch):
    reg = Registry({n: Person(n, 9120 + i, "local", state="active") for i, n in enumerate(["alice", "bob"])})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    monkeypatch.setattr(people, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    scope = {"current": False}
    monkeypatch.setattr(host, "managed_scope_current", lambda s: scope["current"])
    monkeypatch.setattr(host, "write_managed_scope", lambda s: scope.update(current=True))
    for step in ("install_units", "ensure_profile"):
        monkeypatch.setattr(host, step, lambda *a: None)
    monkeypatch.setattr(people, "url_for", lambda s, p: p.url)
    monkeypatch.setattr(people, "configure_webapp", lambda s, p: False)
    restarted = []
    monkeypatch.setattr(people, "_gateway_up", lambda s, p: restarted.append(p.name))

    monkeypatch.setattr(host, "firewall", lambda s: None)

    def tailscale_down(s, reg):
        raise CommandError("tailscale serve -> exit 1")
    monkeypatch.setattr(host, "reconcile_access", tailscale_down)
    with pytest.raises(CommandError):
        apply.apply(S)          # the new policy is written, but nobody was restarted
    assert scope["current"] and restarted == []

    monkeypatch.setattr(host, "reconcile_access", lambda s, reg: None)
    apply.apply(S)              # nothing changes any more, yet the restarts are still owed
    assert restarted == ["alice", "bob"]


@pytest.mark.parametrize("model_lock", [True, False])
def test_turning_the_model_lock_on_first_removes_the_model_people_started_with(tmp_path, monkeypatch, model_lock):
    reg = Registry({"alice": Person("alice", 9120, "local", state="active"),
                    "bob": Person("bob", 9121, "local", state="pending")})   # an add that stopped halfway
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    monkeypatch.setattr(people, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    for step in ("firewall", "install_units", "ensure_profile", "reconcile_access"):
        monkeypatch.setattr(host, step, lambda *a: None)
    monkeypatch.setattr(people, "url_for", lambda s, p: p.url)
    monkeypatch.setattr(people, "configure_webapp", lambda s, p: False)
    monkeypatch.setattr(people, "_gateway_up", lambda s, p: None)
    monkeypatch.setattr(host, "managed_scope_current", lambda s: False)
    done = []
    monkeypatch.setattr(host, "write_managed_scope", lambda s: done.append("policy"))
    monkeypatch.setattr(people, "hermes", lambda name, args, **kw: done.append(f"{name}: hermes {' '.join(args)}"))

    apply.apply(dataclasses.replace(S, model_lock=model_lock))
    # Hermes would drop them from the person's config once pinned, and can leave it unreadable.
    unset = [f"{name}: hermes config unset {key}" for name in ("alice", "bob")
             for key in ("model.provider", "model.default", "agent.reasoning_effort")]
    assert done == (unset if model_lock else []) + ["policy"]
