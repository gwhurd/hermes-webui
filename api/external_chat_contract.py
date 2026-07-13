"""Fail-closed contracts for the Vault external-chat departure slice.

This module deliberately has no logging.  Its inputs can contain private actor
identity and MCP output, neither of which belongs in WebUI diagnostics.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any


_VAULT_REMOVAL_TOOLS = {
    "mcp_vault_mcp_vault_start_removal": "departure",
    "mcp_vault_mcp_vault_record_removal_pickup": "pickup",
    "mcp_vault_mcp_vault_complete_removal": "return",
}
_CARD_KIND = "vault.removal_assignment_confirmation"
_CARD_VERSION = 1
_CARD_KEYS = frozenset({"kind", "version", "command", "issuedAt", "expiresAt", "candidates"})
_CANDIDATE_KEYS = frozenset({
    "assignmentId", "decedentName", "caseNumber", "source", "scheduledFor", "assignedTeam",
})
_CARD_LIFETIME_MS = 300_000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_CONTEXT_REQUEST_KEYS = frozenset({
    "clerk_user_id", "session_id", "command", "card_fingerprint", "candidate_ids",
})
_confirmation_contexts: dict[tuple[str, str, str, str], dict] = {}
_confirmation_contexts_lock = threading.Lock()


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
    clerk_user_id: str | None = None,
) -> list[dict]:
    """Build the external response events, keeping cards machine-authoritative."""
    events = [
        {"type": "delta", "content": final_response[index : index + 80]}
        for index in range(0, len(final_response), 80)
    ]
    messages = result.get("messages") if isinstance(result, dict) else None
    effective_now = now or datetime.now(timezone.utc)
    confirmation_card, command_context = confirmation_card_and_context_from_current_turn(
        messages, now=effective_now
    )
    if confirmation_card is not None:
        if command_context is not None and isinstance(clerk_user_id, str) and clerk_user_id.strip():
            _remember_confirmation_context(
                clerk_user_id.strip(), session_id, confirmation_card, command_context,
                now_ms=_unix_milliseconds(effective_now),
            )
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


def _bounded_trimmed_string(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= maximum else None


def _normalize_command_arguments(command: str, value: Any) -> dict | None:
    """Validate only the public MCP fields and mirror each tool's normalization."""
    if not isinstance(value, dict):
        return None
    if command == "pickup":
        allowed = {
            "decedentQuery", "confirmationAssignmentId", "sourceCustodyAcknowledged",
            "conditionNotes", "accessNotes", "idempotencyKey",
        }
        required = {"decedentQuery", "sourceCustodyAcknowledged", "idempotencyKey"}
    elif command == "return":
        allowed = {"decedentQuery", "confirmationAssignmentId", "destination", "notes", "idempotencyKey"}
        required = {"decedentQuery", "destination", "idempotencyKey"}
    else:
        return None
    if not required.issubset(value) or any(key not in allowed for key in value):
        return None

    decedent_query = _bounded_trimmed_string(value.get("decedentQuery"), 256)
    idempotency_key = _bounded_trimmed_string(value.get("idempotencyKey"), 200)
    if decedent_query is None or idempotency_key is None:
        return None
    normalized = {"decedentQuery": decedent_query}
    confirmation_id = value.get("confirmationAssignmentId")
    if confirmation_id is not None:
        confirmation_id = _bounded_trimmed_string(confirmation_id, 256)
        if confirmation_id is None:
            return None
        normalized["confirmationAssignmentId"] = confirmation_id

    if command == "pickup":
        if value.get("sourceCustodyAcknowledged") is not True:
            return None
        normalized["sourceCustodyAcknowledged"] = True
        for key in ("conditionNotes", "accessNotes"):
            note = value.get(key)
            if note is not None:
                if not isinstance(note, str) or len(note.strip()) > 1000:
                    return None
                if note.strip():
                    normalized[key] = note.strip()
    else:
        destination = _bounded_trimmed_string(value.get("destination"), 200)
        if destination is None:
            return None
        normalized["destination"] = destination
        notes = value.get("notes")
        if notes is not None:
            if not isinstance(notes, str) or len(notes.strip()) > 1000:
                return None
            if notes.strip():
                normalized["notes"] = notes.strip()
    normalized["idempotencyKey"] = idempotency_key
    return normalized


def _tool_call_arguments(call: dict, command: str) -> dict | None:
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
        return None
    try:
        raw_arguments = json.loads(function["arguments"])
    except (TypeError, ValueError):
        return None
    return _normalize_command_arguments(command, raw_arguments)


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
    assignment_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_KEYS:
            return None
        for key in ("assignmentId", "decedentName", "caseNumber", "source", "assignedTeam"):
            if _bounded_trimmed_string(candidate.get(key), 256) != candidate.get(key):
                return None
        scheduled_for = candidate.get("scheduledFor")
        if _bounded_trimmed_string(scheduled_for, 64) != scheduled_for or not isinstance(scheduled_for, str):
            return None
        try:
            parsed_scheduled_for = datetime.fromisoformat(scheduled_for[:-1] + "+00:00") if scheduled_for.endswith("Z") else None
            if parsed_scheduled_for is None or scheduled_for not in {
                parsed_scheduled_for.isoformat(timespec="seconds").replace("+00:00", "Z"),
                parsed_scheduled_for.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            }:
                return None
        except ValueError:
            return None
        assignment_id = candidate["assignmentId"]
        if assignment_id in assignment_ids:
            return None
        assignment_ids.add(assignment_id)
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


def confirmation_card_and_context_from_current_turn(
    messages: Any, *, now: datetime | None = None
) -> tuple[dict | None, dict | None]:
    """Return a validated card from the final user turn's final Vault tool row.

    Assistant prose, historic rows, unrelated tools, and malformed MCP envelopes
    are deliberately ignored.  The final matching row is authoritative so an
    earlier successful-looking result cannot survive a later current-turn result.
    """
    if not isinstance(messages, list):
        return None, None
    current_turn_start = None
    for index in range(len(messages) - 1, -1, -1):
        row = messages[index]
        if isinstance(row, dict) and row.get("role") == "user":
            current_turn_start = index
            break
    if current_turn_start is None:
        return None, None

    called_commands: dict[str, tuple[str, dict | None]] = {}
    matching_rows: list[tuple[dict, str, dict | None]] = []
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
                    called_commands[call_id] = (command, _tool_call_arguments(call, command))
            continue
        tool_name = row.get("name")
        tool_call_id = row.get("tool_call_id")
        binding = called_commands.get(tool_call_id) if isinstance(tool_call_id, str) else None
        command = binding[0] if binding else None
        if (
            row.get("role") == "tool"
            and isinstance(tool_name, str)
            and command is not None
            and _VAULT_REMOVAL_TOOLS.get(tool_name) == command
            and row.get("tool_name", tool_name) == tool_name
        ):
            matching_rows.append((row, command, binding[1]))
    if not matching_rows:
        return None, None
    tool_row, command, command_context = matching_rows[-1]
    card = _approved_card(
        _structured_content_from_tool_row(tool_row),
        now or datetime.now(timezone.utc),
        command,
    )
    if card is None or (command in {"pickup", "return"} and command_context is None):
        return None, None
    return card, command_context


def confirmation_card_from_current_turn(messages: Any, *, now: datetime | None = None) -> dict | None:
    return confirmation_card_and_context_from_current_turn(messages, now=now)[0]


def card_fingerprint(card: dict) -> str:
    canonical = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def clear_confirmation_context_registry_for_tests() -> None:
    with _confirmation_contexts_lock:
        _confirmation_contexts.clear()


def _remember_confirmation_context(
    clerk_user_id: str, session_id: str, card: dict, context: dict, *, now_ms: int
) -> None:
    if card["expiresAt"] <= now_ms:
        return
    candidate_ids = tuple(candidate["assignmentId"] for candidate in card["candidates"])
    confirmation_id = context.get("confirmationAssignmentId")
    if confirmation_id is not None and confirmation_id not in candidate_ids:
        return
    fingerprint = card_fingerprint(card)
    entry = {
        "clerk_user_id": clerk_user_id,
        "session_id": session_id,
        "command": card["command"],
        "card_fingerprint": fingerprint,
        "candidate_ids": candidate_ids,
        "expires_at": card["expiresAt"],
        "context": dict(context),
    }
    with _confirmation_contexts_lock:
        for key, existing in list(_confirmation_contexts.items()):
            if existing["expires_at"] <= now_ms:
                _confirmation_contexts.pop(key, None)
        _confirmation_contexts[(clerk_user_id, session_id, card["command"], fingerprint)] = entry


def confirmation_context_for_request(get_session: Any, payload: Any, *, now_ms: int) -> dict | None:
    """Return only normalized command facts after exact durable and ephemeral binding checks."""
    if not isinstance(payload, dict) or set(payload) != _CONTEXT_REQUEST_KEYS:
        return None
    owner = _bounded_trimmed_string(payload.get("clerk_user_id"), 256)
    session_id = _bounded_trimmed_string(payload.get("session_id"), 256)
    command = payload.get("command")
    fingerprint = payload.get("card_fingerprint")
    candidate_ids = payload.get("candidate_ids")
    if (
        owner is None or session_id is None or command not in {"pickup", "return"}
        or not isinstance(fingerprint, str) or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
        or not isinstance(candidate_ids, list) or not candidate_ids or len(candidate_ids) > 5
        or any(_bounded_trimmed_string(value, 256) != value for value in candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        return None
    try:
        session = get_session(session_id)
    except (KeyError, FileNotFoundError, ValueError):
        return None
    if not vault_external_session_owned_by(session, owner):
        return None
    key = (owner, session_id, command, fingerprint)
    with _confirmation_contexts_lock:
        entry = _confirmation_contexts.get(key)
        if not entry or entry["expires_at"] <= now_ms or entry["candidate_ids"] != tuple(candidate_ids):
            return None
        return dict(entry["context"])
