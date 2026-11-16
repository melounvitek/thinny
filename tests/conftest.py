import pytest

from thinny import host


@pytest.fixture(autouse=True)
def no_dashboard(tmp_path, monkeypatch):
    """Whether this machine runs thinny's dashboard stays out of the tests."""
    monkeypatch.setattr(host, "DASHBOARD_STATE", tmp_path / "dashboard")
