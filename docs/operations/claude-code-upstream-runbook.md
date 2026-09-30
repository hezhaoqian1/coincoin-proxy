---
type: runbook
status: active
owner: platform
audience: [operator, developer, reviewer, agent]
updated: 2026-09-30
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

## Claude Fable 5.1

`claude-fable-5-1` is the current Anthropic model for demanding reasoning and
long-horizon agentic work. It is exposed as a route-only public model and must
use an active `anthropic_compatible` provider channel with the exact upstream
model id `claude-fable-5-1`.

The checked-in base price is 1,000 cents per million input tokens and 5,000
cents per million output tokens. Anthropic prices cache reads for this model at
2.5% of base input, so the catalog uses `cache_read_multiplier: 0.025`.
`claude-fable-5` remains available for existing clients.

The catalog intentionally contains no upstream URL or API key for either Fable
model. Adding the public id therefore does not silently send traffic to a
legacy GPT-backed Claude alias.

Some public ids are spelling aliases for the same provider model. The checked-in
catalog can declare `metadata.provider_route_aliases`; when a public model has no
exact provider-channel route, the router checks those aliases in order. For
example, `claude-opus-4-6` reuses `claude-opus-4.6` routes unless an operator
creates explicit `claude-opus-4-6` routes. Explicit routes always win, so an
operator can still tune or disable the hyphen alias independently.

## Claude Sonnet 5.5

`claude-sonnet-5-5` was released on September 28, 2026. Its catalog entry uses
native Anthropic Messages routing, not the older GPT-backed compatibility alias.
Register an active `anthropic_compatible` provider channel route with endpoint
`chat/completions` and exact upstream model `claude-sonnet-5-5`. Clients use
`/v1/messages`. Discover the model on each upstream before creating its route.

The base price is 200 cents per million input tokens and 1,000 cents per million
output tokens. The catalog defaults to `model_multiplier=4.0`,
`output_multiplier=1.0`, `cache_read_multiplier=0.1`, and
`cache_creation_multiplier=1.25`: effective prices are $8 input, $40 output,
$0.80 cache reads, and $10 cache writes per million tokens. Do not apply an
Opus pricing override to Sonnet. Runtime pricing overrides still take precedence.

After deploying the catalog, use `PATCH /admin/model-pricing/claude-sonnet-5-5`
with `{"model_multiplier":4,"output_multiplier":1,"cache_read_multiplier":0.1}`
to persist the requested production policy. The cache-write multiplier remains
in the catalog. Model-route and pricing admin APIs require an existing public
catalog id; they cannot register an unknown model before catalog deployment.

Sonnet defaults to adaptive thinking with high effort. Native requests retain
their `thinking`, `output_config`, and tool fields, and the upstream validates
Sonnet-specific restrictions. This addition does not add separate 1h cache
write accounting; the existing write rate corresponds to the 5m tier.

Source: https://platform.claude.com/docs/en/models/sonnet-5-5/overview

### Production verification, September 30, 2026

Deployment `1ee226fc-a771-4a9a-a2d0-6620c63f669e` contains only the two new
catalog entries and native `output_config` forwarding, built on the previously
active production commit `4b0deff06184953d36e08d2e4caf7d0b0257502c`. Unrelated
local work was not deployed. Both models appear in `/v1/models`; `/health`
returns 200, and existing models retain their pricing.

One-shot generation probes returned 200 for Sonnet through `Sixoner ccmax 1x`
and for GPT-6.1 Sol through both `2号sixoner稳定` and `sixoner 推荐pro`.
`Flow-Node 满血CCMAX` advertised Sonnet 5.5 but returned 502 on the native
generation probe, so only its new Sonnet 5.5 route was disabled. Existing
routes were left alone. Temporary disabled probe monitors were removed after
the checks; no recurring test monitors or credentials were added to the repo.

Future Git-based deployments must include these catalog entries and effort
forwarding to preserve support for the new models.

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

## Streaming error contract

Anthropic streaming errors are part of the SSE stream contract. After CoinCoin
has started forwarding a stream, the HTTP status may already be `200`; downstream
systems must detect failure from the stream event rather than from a later HTTP
status code.

For native Anthropic Messages upstreams, a mid-stream failure must be forwarded
as an Anthropic error event:

```text
event: error
data: {"type":"error","error":{"type":"api_error","message":"Upstream request failed"}}
```

Preserve official Anthropic error types when the upstream sends one, including
`rate_limit_error`, `overloaded_error`, `timeout_error`, and `api_error`. If the
upstream sends a provider URL, trace id, key name, edge-provider detail, or any
non-Anthropic value as `error.type`, normalize it to `api_error` and sanitize the
message before sending it to clients.

Never emit `event: message_stop` after a stream error. A downstream such as
NewAPI treats a Claude stream payload with `type: "error"` and non-empty
`error.type` as a relay error. If CoinCoin first emits a normal stop event, many
clients will close the parser as a successful completion and ignore the later
error.

CoinCoin records a zero-cost terminal failure log for a stream that ends in an
error event. It must not record successful usage for that stream, even if the
upstream had already sent a `message_start` usage block.

Fallback remains a pre-stream decision only. If the upstream fails after the
gateway has already started forwarding events to the client, the gateway returns
a sanitized stream error event. It does not splice a second provider's stream
into an already-open response.

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
