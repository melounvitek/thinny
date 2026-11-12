import pytest

from hermes_manager.registry import Registry, RegistryError, validate_name


def test_people_get_the_lowest_free_port_and_survive_a_save(tmp_path):
    path = tmp_path / "people.json"
    reg = Registry()
    reg.add("alice", "lan", (9120, 9199))
    reg.add("bob", "tailscale", (9120, 9199))
    reg.save(path)

    loaded = Registry.load(path)
    assert [(p.name, p.port, p.access, p.state) for p in loaded.current()] == [
        ("alice", 9120, "lan", "pending"), ("bob", 9121, "tailscale", "pending")]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_ports_of_disabled_people_are_never_reused():
    reg = Registry()
    reg.add("alice", "lan", (9120, 9121)).state = "disabled"
    assert reg.add("bob", "lan", (9120, 9121)).port == 9121
    assert reg.current() == [reg.get("bob")]
    with pytest.raises(RegistryError, match="no free Webapp port"):
        reg.add("carol", "lan", (9120, 9121))


def test_names_must_be_unique_and_valid():
    reg = Registry()
    reg.add("alice", "lan", (9120, 9199))
    with pytest.raises(RegistryError, match="already exists"):
        reg.add("alice", "lan", (9120, 9199))
    with pytest.raises(RegistryError, match="no person named"):
        reg.get("bob")


@pytest.mark.parametrize("name", ["a", "Alice", "1alice", "alice-", "al_ice", "a" * 33, "../x"])
def test_invalid_names(name):
    with pytest.raises(RegistryError, match="invalid name"):
        validate_name(name)


def test_missing_registry_is_empty(tmp_path):
    assert Registry.load(tmp_path / "people.json").people == {}
