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


def test_vault_requires_nonblank_actor_context_before_sse():
    """Vault turns must reject invalid identity context before SSE begins."""
    body = SRC[SRC.index("def _handle_external_chat") :]
    validation = body[: body.index("# 7. Begin SSE response")]
    assert 'body.get("actor_context")' in validation
    assert 'requested_profile == "vault"' in validation
    assert 'actor_context.get("clerk_user_id")' in validation
    assert 'actor_context.get("convex_token")' in validation
    assert 'not isinstance(clerk_user_id, str)' in validation
    assert 'not clerk_user_id.strip()' in validation
    assert 'not isinstance(convex_token, str)' in validation
    assert 'not convex_token.strip()' in validation
    assert "vault actor context is required" in validation


def test_vault_actor_metadata_is_task_local_and_scoped_to_agent_run():
    """Vault actor identity uses the core's server-scoped ContextVar binder."""
    body = SRC[SRC.index("def _handle_external_chat") : SRC.index("class QuietHTTPServer")]
    chat_lock = body.index("with CHAT_LOCK:")
    agent_run = body.index("result = agent.run_conversation(")
    metadata_scope = body.index("metadata_scope =")
    assert chat_lock < metadata_scope < agent_run
    assert "with metadata_scope:" in body[metadata_scope:agent_run]
    assert "_vault_mcp_metadata_scope(convex_token)" in body
    assert '"vault-mcp"' in SRC
    assert '"com.southwestcremation.vault/convex-user-token"' in SRC


def test_vault_actor_env_propagation_and_restore_logic_are_removed():
    """Vault turns must never place actor identity in process-global env vars."""
    body = SRC[SRC.index("def _handle_external_chat") : SRC.index("class QuietHTTPServer")]
    assert "CONVEX_USER_TOKEN" not in body
    assert "VAULT_USER_ID" not in body
    assert "old_convex_user_token" not in body
    assert "old_vault_user_id" not in body


def test_actor_token_is_not_added_to_messages_sse_or_errors():
    """The actor token is process-only and never enters a persisted/output payload."""
    body = SRC[SRC.index("def _handle_external_chat") : SRC.index("class QuietHTTPServer")]
    run_start = body.index("result = agent.run_conversation(")
    run_end = body.index("\n                    )", run_start)
    assert "convex_token" not in body[run_start:run_end]
    assert "persist_user_message=user_msg" in body
    assert "final_response = _redact_actor_token(" in body
    assert "result_messages = _redact_actor_token(" in body
    # Vault failures must not serialize an exception that could contain env data.
    assert '_send_error("agent error")' in body
    assert 'print("[external-chat] WARNING: resolve_runtime_provider failed"' in body


def test_sse_event_shapes():
    body = SRC[SRC.index("def _handle_external_chat") :]
    contract = (Path(__file__).resolve().parent.parent / "api" / "external_chat_contract.py").read_text()
    assert '"type": "session", "session_id": session_id' in body
    assert "external_turn_events(final_response, result, session_id)" in body
    assert '"type": "delta", "content": final_response[index : index + 80]' in contract
    assert '"type": "done", "content": final_response, "session_id": session_id' in contract
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
    assert "resolve_external_session(" in body
    contract = (Path(__file__).resolve().parent.parent / "api" / "external_chat_contract.py").read_text()
    assert "get_session(session_id)" in contract
    assert "new_session(profile=requested_profile)" in contract
    # unknown session_id falls back to a fresh session instead of 500
    assert "except (KeyError, FileNotFoundError):" in contract


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
