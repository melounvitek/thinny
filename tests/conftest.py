import pytest

from thinny import host, settings


@pytest.fixture(autouse=True)
def no_dashboard(tmp_path, monkeypatch):
    """Whether this machine runs thinny's dashboard stays out of the tests."""
    monkeypatch.setattr(host, "DASHBOARD_STATE", tmp_path / "dashboard")


@pytest.fixture(autouse=True)
def no_secrets(tmp_path, monkeypatch):
    """No test sees this machine's tokens: secrets.env is in tmp_path, and none is in the environment."""
    monkeypatch.setattr(settings, "SECRETS_ENV", tmp_path / "secrets.env")
    for name in settings.SECRETS:
        monkeypatch.delenv(name, raising=False)
