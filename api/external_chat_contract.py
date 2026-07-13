"""Fail-closed contracts for the Vault external-chat departure slice.

This module deliberately has no logging.  Its inputs can contain private actor
identity and MCP output, neither of which belongs in WebUI diagnostics.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


_VAULT_REMOVAL_TOOLS = {
    "mcp_vault_mcp_vault_start_removal": "departure",
    "mcp_vault_mcp_vault_record_removal_pickup": "pickup",
}
_CARD_KIND = "vault.removal_assignment_confirmation"
_CARD_VERSION = 1
_CARD_KEYS = frozenset({"kind", "version", "command", "issuedAt", "expiresAt", "candidates"})
_CANDIDATE_KEYS = frozenset({
    "assignmentId", "decedentName", "caseNumber", "source", "scheduledFor", "assignedTeam",
})
_CARD_LIFETIME_MS = 300_000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def bind_vault_external_session_owner(session: Any, clerk_user_id: str) -> None:
    """Bind a validated Vault actor to a newly server-created session."""
    if not isinstance(clerk_user_id, str) or not clerk_user_id.strip():
        raise ValueError("invalid external session owner")
    session.external_session_owner = clerk_user_id.strip()


def vault_external_session_owned_by(session: Any, clerk_user_id: str) -> bool:
    """Return whether this persisted Vault session belongs to this actor."""
    return (
        getattr(session, "profile", None) == "vault"
        and isinstance(clerk_user_id, str)
        and bool(clerk_user_id.strip())
        and getattr(session, "external_session_owner", None) == clerk_user_id.strip()
    )


def resolve_external_session(
    get_session: Any,
    new_session: Any,
    session_id: str,
    requested_profile: str,
    clerk_user_id: str,
) -> tuple[Any, str]:
    """Resolve an external session and enforce durable Vault actor ownership."""
    created = False
    if session_id:
        try:
            session = get_session(session_id)
        except (KeyError, FileNotFoundError):
            session = new_session(profile=requested_profile)
            session_id = session.session_id
            created = True
    else:
        session = new_session(profile=requested_profile)
        session_id = session.session_id
        created = True

    if requested_profile == "vault":
        if created:
            bind_vault_external_session_owner(session, clerk_user_id)
            session.save()
        elif not vault_external_session_owned_by(session, clerk_user_id):
            raise PermissionError("external session ownership denied")
    return session, session_id


def external_turn_events(
    final_response: str,
    result: Any,
    session_id: str,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Build the external response events, keeping cards machine-authoritative."""
    events = [
        {"type": "delta", "content": final_response[index : index + 80]}
        for index in range(0, len(final_response), 80)
    ]
    messages = result.get("messages") if isinstance(result, dict) else None
    confirmation_card = confirmation_card_from_current_turn(messages, now=now)
    if confirmation_card is not None:
        events.append({
            "type": "confirmation_card",
            "card": confirmation_card,
            "session_id": session_id,
        })
    events.append({"type": "done", "content": final_response, "session_id": session_id})
    return events


def _unix_milliseconds(value: datetime) -> int:
    """Return an aware datetime as an exact Unix-millisecond integer."""
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    utc_value = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc_value - epoch
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _safe_unix_milliseconds(value: Any) -> int | None:
    """Accept only finite JavaScript-safe integer Unix milliseconds."""
    if type(value) is not int or abs(value) > _MAX_SAFE_INTEGER:
        return None
    return value


def _structured_content_from_tool_row(tool_row: dict) -> dict | None:
    """Parse only the MCP result envelope preserved in an AIAgent tool row."""
    content = tool_row.get("content")
    if not isinstance(content, str):
        return None
    try:
        envelope = json.loads(content)
    except (TypeError, ValueError):
        # mcp_* content may have been untrusted-data wrapped by Hermes core.
        marker = "\n\n"
        closing = "\n</untrusted_tool_result>"
        if not content.startswith("<untrusted_tool_result ") or not content.endswith(closing):
            return None
        _, separator, raw_envelope = content.partition(marker)
        if not separator:
            return None
        try:
            envelope = json.loads(raw_envelope[: -len(closing)])
        except (TypeError, ValueError):
            return None
    if not isinstance(envelope, dict):
        return None
    structured = envelope.get("structuredContent")
    return structured if isinstance(structured, dict) else None


def _tool_call_name(call: Any) -> str | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return call.get("name") if isinstance(call.get("name"), str) else None


def _approved_card(payload: Any, now: datetime, command: str) -> dict | None:
    if not isinstance(payload, dict) or set(payload) != _CARD_KEYS:
        return None
    if (
        payload.get("kind") != _CARD_KIND
        or type(payload.get("version")) is not int
        or payload.get("version") != _CARD_VERSION
        or payload.get("command") != command
    ):
        return None
    issued_at = _safe_unix_milliseconds(payload.get("issuedAt"))
    expires_at = _safe_unix_milliseconds(payload.get("expiresAt"))
    now_ms = _unix_milliseconds(now)
    if (
        issued_at is None
        or expires_at is None
        or issued_at > now_ms
        or expires_at <= now_ms
        or expires_at <= issued_at
        or expires_at - issued_at > _CARD_LIFETIME_MS
    ):
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > 5:
        return None
    approved_candidates = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_KEYS:
            return None
        if not all(isinstance(candidate[key], str) and candidate[key].strip() for key in _CANDIDATE_KEYS):
            return None
        approved_candidates.append({key: candidate[key] for key in (
            "assignmentId", "decedentName", "caseNumber", "source", "scheduledFor", "assignedTeam",
        )})
    return {
        "kind": _CARD_KIND,
        "version": _CARD_VERSION,
        "command": command,
        "issuedAt": payload["issuedAt"],
        "expiresAt": payload["expiresAt"],
        "candidates": approved_candidates,
    }


def confirmation_card_from_current_turn(messages: Any, *, now: datetime | None = None) -> dict | None:
    """Return a validated card from the final user turn's final Vault tool row.

    Assistant prose, historic rows, unrelated tools, and malformed MCP envelopes
    are deliberately ignored.  The final matching row is authoritative so an
    earlier successful-looking result cannot survive a later current-turn result.
    """
    if not isinstance(messages, list):
        return None
    current_turn_start = None
    for index in range(len(messages) - 1, -1, -1):
        row = messages[index]
        if isinstance(row, dict) and row.get("role") == "user":
            current_turn_start = index
            break
    if current_turn_start is None:
        return None

    called_commands: dict[str, str] = {}
    matching_rows: list[tuple[dict, str]] = []
    for row in messages[current_turn_start + 1 :]:
        if not isinstance(row, dict):
            continue
        if row.get("role") == "assistant":
            for call in row.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                tool_call_name = _tool_call_name(call)
                command = _VAULT_REMOVAL_TOOLS.get(tool_call_name) if tool_call_name else None
                call_id = call.get("id")
                if command is not None and isinstance(call_id, str) and call_id:
                    called_commands[call_id] = command
            continue
        tool_name = row.get("name")
        tool_call_id = row.get("tool_call_id")
        command = called_commands.get(tool_call_id) if isinstance(tool_call_id, str) else None
        if (
            row.get("role") == "tool"
            and isinstance(tool_name, str)
            and command is not None
            and _VAULT_REMOVAL_TOOLS.get(tool_name) == command
            and row.get("tool_name", tool_name) == tool_name
        ):
            matching_rows.append((row, command))
    if not matching_rows:
        return None
    tool_row, command = matching_rows[-1]
    return _approved_card(
        _structured_content_from_tool_row(tool_row),
        now or datetime.now(timezone.utc),
        command,
    )
