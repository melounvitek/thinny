"""Server policy plugin: only allow the models the administrator approved.

Installed by hermes-manager into the shared install's bundled plugins and enabled from the managed
scope (/etc/hermes/config.yaml), so people cannot disable it from their settings. The allowlist is
/etc/hermes/allowed-models.txt (one model id per line, '#' comments), read on every turn; when it is
missing or empty every turn is blocked (fail closed). The message people see comes from
/etc/hermes/model-guard-message.txt ({model} and {allowed} are filled in).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ALLOWLIST_FILE = Path("/etc/hermes/allowed-models.txt")
MESSAGE_FILE = Path("/etc/hermes/model-guard-message.txt")
DEFAULT_MESSAGE = "Model {model} is not allowed on this server. Allowed: {allowed}."


def _allowed_models() -> set[str]:
    try:
        lines = ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.split("#", 1)[0].strip().lower() for line in lines} - {""}


def _message(model: Any, allowed: set[str]) -> str:
    try:
        template = MESSAGE_FILE.read_text(encoding="utf-8").strip() or DEFAULT_MESSAGE
    except OSError:
        template = DEFAULT_MESSAGE
    shown = ", ".join(sorted(allowed)) or "(none)"
    try:
        return template.format(model=model, allowed=shown)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_MESSAGE.format(model=model, allowed=shown)


def _normalise(model: Any) -> str:
    name = str(model or "").strip().lower()
    return name.rsplit("/", 1)[-1]  # "openai-codex/gpt-x" -> "gpt-x"


def _on_pre_llm_call(model: Any = None, session_id: str = "", **_: Any):
    allowed = _allowed_models()
    if _normalise(model) in allowed:
        return None
    logger.warning("model-guard: blocked turn on model %r (session %s); allowed: %s",
                   model, session_id or "?", sorted(allowed))
    return {"action": "block", "message": _message(model, allowed)}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
