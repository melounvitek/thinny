import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("model_guard", Path(__file__).parent.parent / "files/model-guard/__init__.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture
def allowlist(tmp_path, monkeypatch):
    path = tmp_path / "allowed-models.txt"
    monkeypatch.setattr(guard, "ALLOWLIST_FILE", path)
    monkeypatch.setattr(guard, "MESSAGE_FILE", tmp_path / "missing.txt")
    return path


@pytest.mark.parametrize("allowed, model, blocked", [
    ("gpt-x\n", "gpt-x", False),
    ("gpt-x\n", "openai-codex/gpt-x", False),
    ("vendor/model-a  # comment\n", "vendor/model-a", False),
    ("gpt-x\n", "gpt-y", True),
    ("gpt-x\n", "", True),
    ("", "gpt-x", True),          # empty allowlist: fail closed
])
def test_only_allowed_models_pass(allowlist, allowed, model, blocked):
    allowlist.write_text(allowed)
    result = guard._on_pre_llm_call(model=model)
    assert (result is not None) == blocked
    if blocked:
        assert result["action"] == "block" and f"Model {model} is not allowed" in result["message"]


def test_missing_allowlist_blocks_everything(allowlist):
    assert guard._on_pre_llm_call(model="gpt-x")["action"] == "block"
