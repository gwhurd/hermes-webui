"""Gate tests for POST /api/chat/external (API-key-authenticated SSE chat).

Static source-contract checks (fast, no server spin-up) in the style of
test_worktree_remove.py: the endpoint must be wired in server.py BEFORE the
handle_post fall-through, must bypass browser-session auth only for its own
path, and must emit the documented SSE event shapes.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "server.py").read_text()


def test_external_key_read_from_env():
    assert 'os.environ.get("HERMES_EXTERNAL_API_KEY"' in SRC


def test_disabled_without_key_returns_404():
    body = SRC[SRC.index("def _handle_external_chat") :]
    head = body[: body.index("def _send_error")]
    assert "if not _EXTERNAL_API_KEY:" in head
    assert "status=404" in head


def test_bearer_auth_enforced():
    assert 'auth == f"Bearer {_EXTERNAL_API_KEY}"' in SRC
    body = SRC[SRC.index("def _handle_external_chat") :]
    head = body[: body.index("# 2. Read & parse JSON body")]
    assert "_check_external_auth" in head
    assert "status=401" in head


def test_route_intercepted_before_handle_post():
    """The external route must be handled inside _handle_write, before route_func
    (handle_post) runs, and must bypass check_auth only for its own path."""
    hw = SRC[SRC.index("def _handle_write") : SRC.index("def do_POST")]
    assert '"/api/chat/external"' in hw
    assert 'self.command == "POST"' in hw
    # bypass is conditioned on the exact path...
    assert "not _is_external_chat_post" in hw
    assert "check_auth" in hw
    # ...and the external handler returns before route_func is invoked
    assert hw.index("_handle_external_chat(") < hw.index("route_func(self, parsed)")


def test_messages_array_validated():
    body = SRC[SRC.index("def _handle_external_chat") :]
    assert "messages must be a non-empty array" in body
    assert "no user message found" in body


def test_sse_event_shapes():
    body = SRC[SRC.index("def _handle_external_chat") :]
    assert '"type": "session", "session_id": session_id' in body
    assert '{"type": "delta", "content": chunk}' in body
    assert '"type": "done", "content": final_response, "session_id": session_id' in body
    assert '"type": "error", "content": msg, "session_id": session_id' in body


def test_sse_headers_and_chunked_helper():
    body = SRC[SRC.index("def _handle_external_chat") :]
    assert "text/event-stream" in body
    assert "end_sse_headers(handler)" in body


def test_profile_set_and_always_cleared():
    body = SRC[SRC.index("def _handle_external_chat") : SRC.index("class QuietHTTPServer")]
    assert "set_request_profile(requested_profile)" in body
    # clear_request_profile must run in a finally block so a crash can't leak
    # the request profile onto the worker thread
    assert re.search(r"finally:\s*\n\s*clear_request_profile\(\)", body)


def test_session_resume_or_create():
    body = SRC[SRC.index("def _handle_external_chat") :]
    assert "get_session(session_id)" in body
    assert "new_session(profile=requested_profile)" in body
    # unknown session_id falls back to a fresh session instead of 500
    assert "except (KeyError, FileNotFoundError):" in body


def test_conversation_history_forwarded():
    body = SRC[SRC.index("def _handle_external_chat") :]
    assert "conversation_history=conversation_history" in body
    assert "messages[:-1]" in body


def test_env_mutation_restored_in_finally():
    """TERMINAL_CWD / HERMES_EXEC_ASK / HERMES_SESSION_KEY are process-global;
    the handler must restore them under _ENV_LOCK even when the agent raises."""
    body = SRC[SRC.index("def _handle_external_chat") : SRC.index("class QuietHTTPServer")]
    finally_idx = body.index("finally:")
    tail = body[finally_idx:]
    for var in ("TERMINAL_CWD", "HERMES_EXEC_ASK", "HERMES_SESSION_KEY"):
        assert var in tail, f"{var} not restored in finally block"


def test_agent_errors_streamed_not_raised():
    body = SRC[SRC.index("def _handle_external_chat") : SRC.index("class QuietHTTPServer")]
    assert "_send_error(f\"agent error: {exc}\")" in body
    assert "_CLIENT_DISCONNECT_ERRORS" in body
