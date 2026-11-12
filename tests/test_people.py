import pytest

from hermes_manager import people, system
from hermes_manager.registry import Person, Registry
from hermes_manager.system import CommandError


@pytest.fixture
def gateways(tmp_path, monkeypatch):
    """Records restarts; restarting a name in `broken` fails."""
    restarted, broken = [], set()

    def restart(p):
        if p.name in broken:
            raise CommandError(f"{p.name}: gateway not active")
        restarted.append(p.name)
    monkeypatch.setattr(people, "start_or_restart_gateway", restart)
    monkeypatch.setattr(people, "RESTART_PENDING", tmp_path / "restart-pending")
    monkeypatch.setattr(system, "LOG_FILE", tmp_path / "log")
    return restarted, broken


def registry():
    return Registry({n: Person(n, 9120 + i, "lan", state="active") for i, n in enumerate(["alice", "bob", "carol"])})


def test_gateways_left_by_a_failed_restart_are_restarted_next_time(gateways):
    restarted, broken = gateways
    reg = registry()
    broken.add("bob")
    with pytest.raises(CommandError):
        people.restart_all(reg.current(), reg)
    assert restarted == ["alice"]

    broken.clear()
    people.restart_all([], reg)   # e.g. an apply that changed nothing
    assert restarted == ["alice", "bob", "carol"]
    people.restart_all([], reg)
    assert restarted == ["alice", "bob", "carol"]


def test_disabled_people_are_not_restarted(gateways):
    restarted, _ = gateways
    reg = registry()
    reg.get("bob").state = "disabled"
    people.restart_all(list(reg.people.values()), reg)
    assert restarted == ["alice", "carol"]
