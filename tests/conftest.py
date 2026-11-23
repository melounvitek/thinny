import pytest

from thinny import host, instances, settings


@pytest.fixture(autouse=True)
def no_dashboard(tmp_path, monkeypatch):
    """Whether this machine runs thinny's dashboard stays out of the tests."""
    monkeypatch.setattr(host, "DASHBOARD_STATE", tmp_path / "dashboard")


@pytest.fixture(autouse=True)
def readable_pool_names(tmp_path, monkeypatch):
    """The tests read each instance's pool as thinny-<name>, and test_a_pool_does_not_name_its_instance the real
    names, from a key in tmp_path."""
    monkeypatch.setattr(instances, "POOL_KEY", tmp_path / "pool-key")
    monkeypatch.setattr(instances, "pool_of", lambda name: f"thinny-{name}")


@pytest.fixture(autouse=True)
def no_secrets(tmp_path, monkeypatch):
    """No test sees this machine's tokens: secrets.env is in tmp_path, and none is in the environment."""
    monkeypatch.setattr(settings, "SECRETS_ENV", tmp_path / "secrets.env")
    for name in settings.SECRETS:
        monkeypatch.delenv(name, raising=False)
