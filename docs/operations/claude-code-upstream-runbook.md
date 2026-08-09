---
type: runbook
status: active
owner: platform
audience: [operator, developer, reviewer, agent]
updated: 2026-08-09
canonical_for: claude-code-upstream-operations
---

# Claude Code Upstream Runbook

This runbook documents CoinCoin's Claude Code upstream behavior. It intentionally
does not include upstream API keys or admin tokens.

## Runtime shape

Claude Code upstreams are configured as provider channels and reached through
model routes instead of a legacy GPT-backed Claude catalog path.

- Channel type: `anthropic_compatible`
- Route endpoint: `chat/completions`
- Public request endpoint: `/v1/messages`
- Required upstream request shape: Anthropic Messages request, including Claude
  Code headers when the channel is Claude Code-only

The public model remains a CoinCoin model id. The route decides which upstream
model name is sent to the provider.

## Runtime concurrency

Railway runs uvicorn with worker processes. HTTP text clients are per process, so
`COINCOIN_HTTP_POOL_MAX` is a per-worker limit, not a deployment total.

Current repository defaults:

- `WEB_CONCURRENCY`: `2` through the Railway start command default
- `COINCOIN_HTTP_POOL_MAX`: `1000` per worker
- Approximate total outbound text slots: `2 * 1000 = 2000`
- `COINCOIN_HTTP_POOL_KEEPALIVE`: `200` per worker

Keepalive should stay materially lower than max connections so the process does
not retain thousands of idle upstream sockets. Raising the local pool only
reduces gateway-side queuing; it does not raise upstream account concurrency. If
an upstream has a low account limit, prefer additional healthy routes plus
fallback over continually increasing local connection capacity.

Set the Railway variable explicitly when changing the worker count:

```bash
WEB_CONCURRENCY=2
```

The repository `railway.toml` default is safe when the variable is absent, but
the Railway environment variable is still the clearest operational source of
truth.

## Claude Messages fallback

Claude Code traffic enters CoinCoin through `/v1/messages`; routing still uses
the `chat/completions` model-route endpoint because Claude public models share
the same route registry.

Before any bytes are sent to the client, `/v1/messages` should immediately try
the next eligible provider-channel route when the current Claude channel returns
a retryable upstream failure:

- HTTP `401` or `403` from a selected provider channel. User authentication has
  already completed before route selection, so these statuses mean the upstream
  account/key/channel failed, not that the CoinCoin user should be rejected.
- HTTP `429`
- HTTP `502`
- HTTP `503`
- HTTP `504`, including CDN timeout-style failures such as `524`
- upstream connection, connect timeout, read timeout, write timeout, invalid JSON,
  unexpected content type, or empty response errors

Fallback metadata must be visible in request logs:

- successful fallback request has `route_attempt > 0`
- `channel_id` is the final successful channel
- `fallback_from_channel_id` includes failed channel ids
- `route_reason` starts with `channel_fallback:`

Local gateway pool exhaustion is different. `httpx.PoolTimeout` means the
gateway process could not obtain a local outbound connection quickly enough; it
returns a local overload error and should not burn fallback channels for the same
request.

Streaming caveat: if the upstream fails after the gateway has already started
forwarding events to the client, the gateway returns a sanitized stream error
event. It does not splice a second provider's stream into an already-open
response.

The stream replay boundary is intentionally earlier than "HTTP 200". Anthropic
compatible upstreams can return HTTP 200 and then immediately emit an SSE
`event: error`. CoinCoin now pre-reads the upstream stream until the first
client-visible event:

- If HTTP status, transport, empty-stream, invalid native SSE, or native
  `event: error` fails before the first forwarded event, the request is retried
  on the next eligible Claude channel.
- If `message_start`, content, thinking, or tool-use bytes have already been
  forwarded, the request is terminal for that client response. CoinCoin records
  the failed attempt and emits a short sanitized SSE error with the CoinCoin
  request id.

This keeps Claude Code safe from duplicate content/tool calls while still
recovering from providers that fail at stream startup.

When reading logs:

- `route_attempt=0` with a terminal `messages:stream` failure usually means the
  stream failed after CoinCoin had already forwarded a visible event.
- `route_attempt>0` on the final success means at least one earlier Claude
  channel failed before the client-visible stream boundary.
- Intermediate failed attempts should have `request_log_only=true`,
  `requests=0`, zero token usage, and the failed `channel_id`.

## Verification commands

Run the Claude compatibility tests:

```bash
PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/pycache \
COINCOIN_DB_HOST=localhost \
COINCOIN_DB_NAME=test \
COINCOIN_DB_USER=test \
COINCOIN_DB_PASSWORD=test \
.venv/bin/python -m unittest tests.test_anthropic_compat -v
```

Run the OpenAI compatibility tests that cover the shared HTTP transport:

```bash
PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/pycache \
COINCOIN_DB_HOST=localhost \
COINCOIN_DB_NAME=test \
COINCOIN_DB_USER=test \
COINCOIN_DB_PASSWORD=test \
.venv/bin/python -m unittest tests.test_openai_compat_defaults -v
```
