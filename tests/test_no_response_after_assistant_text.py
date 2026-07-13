"""Regression: a stale generic no-response event must not replace streamed prose.

A completed plain-text assistant answer can be rendered before a trailing generic
``no_response``/``silent_failure`` SSE event arrives.  That generic fallback is
not a typed provider error, so the client must settle from the canonical session
instead of rendering a misleading provider-error card over the answer.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _apperror_handler() -> str:
    start = MESSAGES_JS.find("source.addEventListener('apperror',e=>{")
    end = MESSAGES_JS.find("source.addEventListener('warning',e=>{", start)
    assert start >= 0 and end > start, "main apperror handler not found"
    return MESSAGES_JS[start:end]


def test_generic_no_response_after_rendered_assistant_text_settles_without_error_card():
    """Only generic no-response events after real rendered prose are suppressed."""
    handler = _apperror_handler()

    assert "d.type==='no_response'||d.type==='silent_failure'" in handler
    assert "String(assistantText||'').trim()" in handler
    assert "_restoreSettledSession(source" in handler

    guard = handler.index("String(assistantText||'').trim()")
    terminal = handler.index("_terminalStateReached=true")
    error_card = handler.index("S.messages.push({role:'assistant',content:`**${label}:**")
    assert guard < terminal < error_card, (
        "a generic no_response/silent_failure after rendered prose must settle before "
        "the terminal error-card fallback can render"
    )


def test_empty_or_typed_provider_errors_remain_on_the_error_card_path():
    """The suppression is limited to generic no-response kinds with live prose."""
    handler = _apperror_handler()

    assert "d.type==='no_response'||d.type==='silent_failure'" in handler
    assert "d.type==='rate_limit'" in handler
    assert "d.type==='auth_mismatch'" in handler
    assert "d.type==='model_not_found'" in handler
    assert "S.messages.push({role:'assistant',content:`**${label}:**" in handler
