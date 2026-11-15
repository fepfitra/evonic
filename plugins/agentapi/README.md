# Agent API Plugin

Expose Evonic agents via an OpenAI-compatible REST API. Bearer-token-authenticated chat completions with quota and model-scoping.

## Configuration

The plugin requires a single config variable: **MODEL_AGENT_MAP**, a JSON object that maps public model names (keys) to internal Evonic agent IDs (values).

```json
{
  "gpt-4-assistant": "my-assistant",
  "gpt-4-researcher": "my-researcher",
  "gpt-3.5-support": "my-support"
}
```

Configure it in the Evonic admin panel under **Plugins → Agent API → Variables**, or via `plugin.json`.

---

## Consumer Endpoints

These endpoints use **Bearer token** authentication. No session is required — they are designed for external API consumers.

---

### `POST /plugin/agentapi/v1/chat/completions`

OpenAI-compatible chat completion. Routes the conversation to the agent mapped to the requested `model` field.

**Request:**

```bash
curl -X POST http://localhost:8080/plugin/agentapi/v1/chat/completions \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4-assistant",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Hello! Who are you?"}
    ],
    "stream": false
  }'
```

**Response (200):**

```json
{
  "id": "chatcmpl-a1b2c3d4e5f6",
  "object": "chat.completion",
  "created": 1715350000,
  "model": "gpt-4-assistant",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! I am an Evonic agent deployed via the Agent API."
      },
      "finish_reason": "stop"
    }
  ]
}
```

**Streaming mode (`"stream": true`):**

```bash
curl -X POST http://localhost:8080/plugin/agentapi/v1/chat/completions \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4-assistant",
    "messages": [
      {"role": "user", "content": "Tell me a story."}
    ],
    "stream": true
  }'
```

Returns an SSE (Server-Sent Events) stream with `data:` lines containing OpenAI-compatible chunk objects, ending with `data: [DONE]`.

**Session behavior (stateless by default):**

By default, each API call starts with a **clean session** — the agent has no memory of previous calls. This is stateless mode.

| Behavior | Header |
|---|---|
| Stateless (default) | No `X-Session-Id` |
| Stateful (opt-in) | `X-Session-Id: <your-session-id>` |

To enable conversation continuity across requests, pass a custom session ID via the `X-Session-Id` header. All calls with the same ID share the same session context:

```bash
curl -X POST http://localhost:8080/plugin/agentapi/v1/chat/completions \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: my-custom-session-123" \
  -d '{
    "model": "gpt-4-assistant",
    "messages": [
      {"role": "user", "content": "Remember my name is Alice."}
    ]
  }'
```

**Error responses:**

| Status | Meaning |
|--------|---------|
| `401` | Missing or invalid Authorization header |
| `403` | Token suspended, expired, or model not authorized |
| `429` | Quota exceeded (includes `quota_limit` and `quota_used` fields) |
| `400` | Invalid JSON, unknown model, or empty messages |
| `404` | Agent not found |
| `503` | Agent is disabled |

---

### `GET /plugin/agentapi/v1/models`

List available models filtered by the token's scope.

**Request:**

```bash
curl http://localhost:8080/plugin/agentapi/v1/models \
  -H "Authorization: Bearer <your-token>"
```

**Response (200):**

```json
{
  "object": "list",
  "data": [
    {"id": "gpt-4-assistant", "object": "model", "created": 0, "owned_by": "evonic"},
    {"id": "gpt-4-researcher", "object": "model", "created": 0, "owned_by": "evonic"},
    {"id": "gpt-3.5-support", "object": "model", "created": 0, "owned_by": "evonic"}
  ]
}
```

If the token has `allowed_models: ["*"]`, all configured models are returned.

---

### `POST|DELETE /plugin/agentapi/v1/provision`

Create (or remove) an agent for an external platform that has no evonic session —
the one agent-shaped thing a project needs, minted through the door the caller's
bearer token already opens. Used by the Pitura dashboard's instant create
(`server-rs/src/create_project.rs` `Provisioner`).

**Fleet token only.** A token whose `allowed_models` is a list is scoped to
existing agents and answers `403` here, so a leaked project token cannot make
new agents.

**Request (POST):**

```bash
curl -X POST http://localhost:8080/plugin/agentapi/v1/provision \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"slug": "warung-bu-tini", "name": "Warung Bu Tini", "source": "demo"}'
```

`slug` is the platform's project subdomain (lowercase alnum and hyphens); the
agent id is that slug with `-` → `_` (evonic ids are `^[a-z0-9_]+$`) and the
model alias is the agent id. `source` names the agent whose settings and tool
set are cloned; default `demo`.

**Response (201):**

```json
{"agent_id": "warung_bu_tini", "model": "warung_bu_tini", "token": "…43 chars…"}
```

The token is plaintext, scoped to `[model]`, and returned **once** — the caller
stores it. `409` when the agent id already exists (an agent belongs to one
project, so the caller rolls back instead of adopting it), `400` for a bad slug
or an unknown `source`.

**Delete:** `{"slug": "warung-bu-tini"}` (body or `?slug=`) answers
`{"removed": true|false}` and is idempotent; it also drops the agent's token,
its `MODEL_AGENT_MAP` alias and its directories — the rollback for a create
that failed after the agent existed.

---

## Admin Endpoints

These endpoints use the **web session** authentication (cookies) — the same session used when logged into the Evonic admin panel.

**Session setup for curl:**

```bash
# Login first to obtain a session cookie
curl -c cookies.txt -X POST http://localhost:8080/api/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password"}'

# Subsequent requests use the saved cookie
curl -b cookies.txt http://localhost:8080/api/agentapi/admin/tokens
```

---

### `GET /api/agentapi/admin`

Renders the token management dashboard (HTML page).

```bash
curl -b cookies.txt http://localhost:8080/api/agentapi/admin
```

---

### `GET /api/agentapi/admin/tokens`

List all bearer tokens. Optionally filter by status.

```bash
# List all tokens
curl -b cookies.txt http://localhost:8080/api/agentapi/admin/tokens

# Filter by status
curl -b cookies.txt "http://localhost:8080/api/agentapi/admin/tokens?status=active"
curl -b cookies.txt "http://localhost:8080/api/agentapi/admin/tokens?status=suspended"
```

**Response (200):**

```json
{
  "tokens": [
    {
      "id": 1,
      "name": "Production API Key",
      "token_prefix": "abc12345",
      "quota_limit": 10000,
      "quota_used": 342,
      "status": "active",
      "expires_at": "2025-12-31T23:59:59+00:00",
      "allowed_models": ["gpt-4-assistant", "gpt-4-researcher"],
      "last_used_at": "2025-05-15T10:30:00+00:00",
      "created_at": "2025-01-01T00:00:00+00:00"
    }
  ]
}
```

---

### `POST /api/agentapi/admin/tokens`

Create a new bearer token. The plaintext token is returned **only once** — store it securely.

**Request:**

```bash
curl -b cookies.txt -X POST http://localhost:8080/api/agentapi/admin/tokens \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Staging API Key",
    "quota_limit": 5000,
    "expires_at": "2025-12-31T23:59:59Z",
    "allowed_models": ["gpt-4-assistant"]
  }'
```

All fields except `name` are optional:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | — | **Required.** Human-readable label for the token |
| `quota_limit` | integer | `null` (unlimited) | Maximum number of requests this token can make (resets daily) |
| `expires_at` | string (ISO 8601) | `null` (never) | Expiration date/time |
| `allowed_models` | string[] or `"*"` | `"*"` (all models) | Models this token is authorized to use |

**Response (201):**

```json
{
  "token": {
    "id": 2,
    "name": "Staging API Key",
    "token_prefix": "def56789",
    "quota_limit": 5000,
    "quota_used": 0,
    "status": "active",
    "expires_at": "2025-12-31T23:59:59+00:00",
    "allowed_models": ["gpt-4-assistant"],
    "last_used_at": null,
    "created_at": "2025-05-15T15:55:00+00:00"
  },
  "plaintext": "abc-def-ghi-jkl-mno-pqr-stu-vwx-yz"
}
```

> **Important:** Save the `plaintext` value immediately. After this response, it is never returned again unless you use the `/reset` or `/reveal` endpoint (see below).

---

### `PUT /api/agentapi/admin/tokens/<id>`

Update a token's mutable fields. All fields are optional — only supplied fields are changed.

**Request:**

```bash
curl -b cookies.txt -X PUT http://localhost:8080/api/agentapi/admin/tokens/1 \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Key Name",
    "quota_limit": 20000,
    "status": "suspended",
    "expires_at": "2026-06-30T23:59:59Z",
    "allowed_models": ["gpt-4-assistant", "gpt-3.5-support"]
  }'
```

**Response (200):**

```json
{
  "token": {
    "id": 1,
    "name": "Updated Key Name",
    "quota_limit": 20000,
    "status": "suspended",
    ...
  }
}
```

---

### `DELETE /api/agentapi/admin/tokens/<id>`

Permanently delete a token.

```bash
curl -b cookies.txt -X DELETE http://localhost:8080/api/agentapi/admin/tokens/1
```

**Response:** `204 No Content`

---

### `GET /api/agentapi/admin/tokens/<id>/stats`

Return detailed usage statistics for a specific token.

```bash
curl -b cookies.txt http://localhost:8080/api/agentapi/admin/tokens/1/stats
```

**Response (200):**

```json
{
  "stats": {
    "id": 1,
    "name": "Production API Key",
    "token_prefix": "abc12345",
    "quota_limit": 10000,
    "quota_used": 342,
    "status": "active",
    "expires_at": "2025-12-31T23:59:59+00:00",
    "allowed_models": ["gpt-4-assistant"],
    "created_at": "2025-01-01T00:00:00+00:00",
    "usage_log": [
      {
        "agent_id": "my-assistant",
        "model": "gpt-4-assistant",
        "session_id": "api:abc12345:my-assistant",
        "prompt_tokens": 150,
        "completion_tokens": 420,
        "duration_ms": 3800,
        "created_at": "2025-05-15T10:30:00+00:00"
      }
    ]
  }
}
```

---

### `GET /api/agentapi/admin/tokens/<id>/reveal`

Retrieve the cached plaintext token. This is only available after creation or reset — the cache is in-memory and may expire.

```bash
curl -b cookies.txt http://localhost:8080/api/agentapi/admin/tokens/1/reveal
```

**Response (200):**

```json
{
  "plaintext": "abc-def-ghi-jkl-mno-pqr-stu-vwx-yz"
}
```

**Response (404) — expired cache:**

```json
{
  "error": "Plaintext token no longer available"
}
```

---

### `POST /api/agentapi/admin/tokens/<id>/reset`

Regenerate (rotate) a token's secret key. The old key is **immediately invalidated**. All other settings (name, quota, models, status, etc.) are preserved. The new plaintext is returned once.

```bash
curl -b cookies.txt -X POST http://localhost:8080/api/agentapi/admin/tokens/1/reset
```

**Response (200):**

```json
{
  "plaintext": "new-token-value-xyz789"
}
```

> **Important:** Save the new plaintext value immediately. API clients using the old key will lose access immediately.

---

### `GET /api/agentapi/admin/model-agent-map`

Return the current MODEL_AGENT_MAP configuration. Used internally by the admin dashboard.

```bash
curl -b cookies.txt http://localhost:8080/api/agentapi/admin/model-agent-map
```

**Response (200):**

```json
{
  "map": {
    "gpt-4-assistant": "my-assistant",
    "gpt-4-researcher": "my-researcher",
    "gpt-3.5-support": "my-support"
  }
}
```

---

## Token Lifecycle

1. **Create** — Admin creates a token. Plaintext returned once.
2. **Use** — Consumer sends requests with `Authorization: Bearer <plaintext>`.
3. **Monitor** — Admin views stats, quota usage, and last-used timestamps.
4. **Suspend** — Admin sets `status: "suspended"` to temporarily block a token.
5. **Reset** — Admin regenerates the secret key (old key invalidated). New plaintext returned once.
6. **Delete** — Admin permanently removes the token and its usage logs.

## Error Response Format

All error responses follow this structure:

```json
{
  "error": "Human-readable error message"
}
```

Quota-exceeded errors (429) include extra fields:

```json
{
  "error": "Quota exceeded",
  "quota_limit": 10000,
  "quota_used": 10000
}
```
