import pytest

from hermes_manager import apply, host, people, system
from hermes_manager.registry import Person, Registry
from hermes_manager.settings import Settings
from hermes_manager.system import CommandError

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
    for step in ("install_units", "ensure_profile", "reconcile_access"):
        monkeypatch.setattr(host, step, lambda *a: None)
    monkeypatch.setattr(people, "url_for", lambda s, p: p.url)
    monkeypatch.setattr(people, "configure_webapp", lambda s, p: False)
    restarted = []
    monkeypatch.setattr(people, "_gateway_up", lambda p: restarted.append(p.name))

    def firewall_down(s):
        raise CommandError("nft -> exit 1")
    monkeypatch.setattr(host, "firewall", firewall_down)
    with pytest.raises(CommandError):
        apply.apply(S)          # the new policy is written, but nobody was restarted
    assert restarted == []

    monkeypatch.setattr(host, "firewall", lambda s: None)
    apply.apply(S)              # nothing changes any more, yet the restarts are still owed
    assert restarted == ["alice", "bob"]
