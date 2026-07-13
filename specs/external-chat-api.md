# WebUI External Chat API Endpoint

> Add a streaming `/api/chat/external` endpoint to `~/hermes-webui/server.py`
> that Vault's Clear Mode can call over Tailscale for streaming Hermes responses.

---

## Problem

The Hermes WebUI at `localhost:8787` already has a full streaming chat infrastructure
(POST `/api/chat/start` + SSE stream, POST `/api/chat` sync fallback). But these
require browser session auth (cookies, CSRF tokens). Vault (hosted on Vercel) needs
a simple API-key-authenticated endpoint it can call over Tailscale.

## What to Build

Add a single new POST handler to `~/hermes-webui/server.py`:

### POST `/api/chat/external`

**Headers:**
```
Authorization: Bearer <HERMES_EXTERNAL_API_KEY>
Content-Type: application/json
```

**Body:**
```json
{
  "messages": [
    {"role": "user", "content": "Show my cases"}
  ],
  "session_id": "optional-existing-session-id",
  "profile": "vault"
}
```

**Response:** Server-Sent Events (SSE) stream:
```
data: {"type": "delta", "content": "Let me"}

data: {"type": "delta", "content": " look that up"}

data: {"type": "done", "content": "Let me look that up for you."}

data: {"type": "error", "content": "message"}
```

### External SSE wire protocol (authoritative)

This endpoint uses **data-only SSE frames**. It does not use named SSE
`event:` fields: every frame is exactly `data: <JSON object>\n\n`, and the JSON
object's required `type` field is the event discriminator. Consumers must not
expect a named `confirmation_card` SSE event.

The emitted objects are exactly:

```json
{"type":"session","session_id":"opaque-server-session-id"}
{"type":"delta","content":"response text chunk"}
{"type":"confirmation_card","session_id":"opaque-server-session-id","card":{"kind":"vault.removal_assignment_confirmation","version":1,"command":"departure","issuedAt":1783943940000,"expiresAt":1783944240000,"candidates":[{"assignmentId":"opaque-assignment-id","decedentName":"Jane Henderson","caseNumber":"25-001","source":"St. John's Hospital","scheduledFor":"2026-07-13T10:30:00.000Z","assignedTeam":"North Team"}]}}
{"type":"done","content":"complete response text","session_id":"opaque-server-session-id"}
{"type":"error","content":"safe error message","session_id":"opaque-server-session-id"}
```

`confirmation_card` is emitted at most once, after all `delta` objects and
before `done`. It is authoritative only when the current turn's matching
Vault removal MCP result (`vault_start_removal`, `vault_record_removal_pickup`,
or `vault_complete_removal`) has an approved `structuredContent` envelope;
assistant prose, JSON-looking prose, and historic results never create a card.

### POST `/api/chat/external/confirmation-context`

This API-key-authenticated, server-to-server lookup exists only for `pickup`
and `return` cards. Vault submits the exact object
`{clerk_user_id, session_id, command, card_fingerprint, candidate_ids}`. The
WebUI returns normalized original command facts only when the persisted Vault
session owner, command, canonical SHA-256 card fingerprint, ordered unique
candidate IDs, and upstream expiry all match. Disabled service returns 404,
invalid API key returns 401, and every unknown/malformed/stale/mismatched
request returns generic 404. The browser never receives these facts or lookup
tokens; departure is context-free. This endpoint intentionally reuses the
existing allowlisted CORS behavior and adds no permissive CORS header.

The `card` is a strict allowlisted projection of the MCP
`structuredContent`: it has exactly the keys shown above and every candidate
has exactly the six keys shown above. `issuedAt` and `expiresAt` are finite
JavaScript-safe integer Unix milliseconds (not ISO strings):
`issuedAt <= server now`, `expiresAt > server now`, and
`expiresAt - issuedAt <= 300000`. Empty, malformed, expired, future-issued,
extra-field, or over-five-candidate cards are omitted rather than truncated or
inferred. Event payloads never include the actor ID, Convex token, organization
or case ID, raw tool result, or request authorization header.

**On invalid auth:** Return 401 with `{"error": "unauthorized"}`

## Implementation

### 1. Add env var
The API key comes from `HERMES_EXTERNAL_API_KEY` environment variable.
If not set, the endpoint returns 404 (disabled).

### 2. Add handler function to server.py

In `~/hermes-webui/server.py`, add a new handler function and register it
in the POST routing. The file already has this pattern — look at how
the existing CSP report handler works (`_handle_csp_report`).

The handler should:
1. Check `Authorization` header matches `HERMES_EXTERNAL_API_KEY`
2. Parse JSON body
3. Extract `messages`, `session_id` (optional), `profile` (default "default")
4. Create or resume a Hermes session with the requested profile
5. Process the messages through the agent synchronously (use the AIAgent
   pattern from `_handle_chat_sync` in `api/routes.py`)
6. Stream the response as SSE with `data: {...}\n\n` framing
7. Return the final response

### Key pattern to follow

The `_handle_chat_sync` function in `api/routes.py` (line 20107) shows how to:
- Get/create a session
- Configure the agent model/provider
- Create an AIAgent and process messages
- Return the response

For streaming, look at `_handle_sse_stream` (line 12336) for the SSE pattern.

**Important:** The handler goes in `server.py` itself, not `api/routes.py`.
The server.py file already imports from the api module so it can use the
session/agent infrastructure.

### Auth check

Keep it simple:
```python
_EXTERNAL_API_KEY = os.environ.get("HERMES_EXTERNAL_API_KEY", "")

def _check_external_auth(handler) -> bool:
    if not _EXTERNAL_API_KEY:
        return False  # disabled
    auth = handler.headers.get("Authorization", "")
    return auth == f"Bearer {_EXTERNAL_API_KEY}"
```

## Things to watch for

- The AIAgent class import: `from run_agent import AIAgent`
- Session creation: `from routes import create_session` or similar
- Profile setting: use the profile cookie pattern from server.py
- Don't modify routes.py — it's 12K+ lines and complex
- SSE must flush after each `data:` line so Vault sees it in real-time

## Files

| File | Action |
|------|--------|
| `~/hermes-webui/server.py` | Add handler + route + header processing |
| `~/hermes-webui/.env` (optional) | Add `HERMES_EXTERNAL_API_KEY` |

## Verification

1. Set `HERMES_EXTERNAL_API_KEY=test-key` in env
2. Restart WebUI
3. `curl -N -X POST http://127.0.0.1:8787/api/chat/external \
     -H 'Authorization: Bearer test-key' \
     -H 'Content-Type: application/json' \
     -d '{"messages":[{"role":"user","content":"say hi"}],"profile":"default"}'`
4. Should stream SSE events

Commit only after the focused contract tests and relevant session-persistence
tests pass locally. Do not restart, deploy, or push as part of this slice.
