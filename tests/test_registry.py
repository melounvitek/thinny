import pytest

from thinny.registry import Registry, RegistryError, validate_name


def test_instances_get_the_lowest_free_port_and_survive_a_save(tmp_path):
    path = tmp_path / "instances.json"
    reg = Registry()
    reg.add("alice", "lan", (9120, 9199))
    reg.add("bob", "tailscale", (9120, 9199))
    reg.save(path)

    loaded = Registry.load(path)
    assert [(inst.name, inst.port, inst.access, inst.state) for inst in loaded.current()] == [
        ("alice", 9120, "lan", "pending"), ("bob", 9121, "tailscale", "pending")]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_any_account_can_enter_the_state_directory_but_not_list_it(tmp_path):
    path = tmp_path / "thinny" / "instances.json"
    Registry().save(path)
    assert oct(path.parent.stat().st_mode & 0o777) == "0o711"
    path.parent.chmod(0o700)    # as before
    Registry().save(path)
    assert oct(path.parent.stat().st_mode & 0o777) == "0o711"


def test_ports_of_disabled_instances_are_never_reused():
    reg = Registry()
    reg.add("alice", "lan", (9120, 9121)).state = "disabled"
    assert reg.add("bob", "lan", (9120, 9121)).port == 9121
    assert reg.current() == [reg.get("bob")]
    with pytest.raises(RegistryError, match="no free Webapp port"):
        reg.add("carol", "lan", (9120, 9121))


def test_container_addresses_are_never_reused(tmp_path):
    reg = Registry()
    reg.add("alice", "lan", (9120, 9199)).state = "disabled"
    reg.add("bob", "lan", (9120, 9199))
    reg.save(tmp_path / "instances.json")
    loaded = Registry.load(tmp_path / "instances.json")
    assert [inst.address for inst in loaded.instances.values()] == ["10.78.0.1", "10.78.0.2"]
    assert loaded.add("carol", "lan", (9120, 9199)).address == "10.78.0.3"


def test_names_must_be_unique_and_valid():
    reg = Registry()
    reg.add("alice", "lan", (9120, 9199))
    with pytest.raises(RegistryError, match="already exists"):
        reg.add("alice", "lan", (9120, 9199))
    with pytest.raises(RegistryError, match="no instance named"):
        reg.get("bob")


@pytest.mark.parametrize("name", ["a", "Alice", "1alice", "alice-", "al_ice", "a" * 33, "../x"])
def test_invalid_names(name):
    with pytest.raises(RegistryError, match="invalid name"):
        validate_name(name)


def test_missing_registry_is_empty(tmp_path):
    assert Registry.load(tmp_path / "instances.json").instances == {}
