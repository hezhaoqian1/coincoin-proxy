---
type: runbook
status: active
owner: platform
audience: [operator, developer, reviewer, agent]
updated: 2026-08-10
canonical_for: claude-code-upstream-operations
---

# Claude Code Upstream Runbook

This runbook documents the runtime setup for Claude Code-only upstreams in CoinCoin. It intentionally does not include upstream API keys or admin tokens.

## Runtime Shape

Claude Code upstreams are configured as provider channels and reached through model routes instead of the legacy GPT-backed Claude catalog path.

- Channel type: `anthropic_compatible`
- Auth style: `bearer`
- Provider platform: `sixoner`
- Cost tier: `claude-code`
- Provider account fingerprint: `sixoner-claude-code-only`
- Required upstream request shape: Anthropic Messages request with Claude Code headers and `?beta=true`

The public model remains a CoinCoin model id. The route decides which upstream model name is sent to the provider.

## Current Sixoner Claude Code Channel

Production channel:

- Channel id: `ch_360294872e2c6ef54b880615`
- Channel name: `Sixoner Claude Code`
- Base URL: `https://sub.sixoner.com`
- Channel status: `active`

The channel is intended for Claude Code traffic. Ordinary OpenAI-compatible requests or generic server-side probes can be rejected by the upstream's edge controls even when real Claude Code traffic works.

## Public Claude Models

The Claude Code family is exposed through public `claude-*` model ids. The Sonnet set includes:

- `claude-sonnet-4`
- `claude-sonnet-4-6`
- `claude-sonnet-4.5`
- `claude-sonnet-4.6`
- `claude-sonnet-5`

`claude-sonnet-5` must have an active route to the Sixoner channel:

- `public_model_id`: `claude-sonnet-5`
- `endpoint`: `chat/completions`
- `channel_id`: `ch_360294872e2c6ef54b880615`
- `upstream_model`: `claude-sonnet-5`
- `transform_profile`: `anthropic_messages`
- `status`: `active`

Claude public models should remain route-only for Claude Code upstream coverage. Do not silently fall back to GPT-backed Claude aliases for these models.

## Pricing Multiplier Policy

Claude Code public models use model-level pricing overrides in `/admin/model-pricing/{model_id}`.

Current production policy:

- `model_multiplier`: `4.0`
- `output_multiplier`: `1.0`
- `cache_read_multiplier`: `0.1`
- `pricing_mode`: `multiplier`

The router computes effective prices as:

- input price = `base_input * model_multiplier`
- output price = `base_output * model_multiplier * output_multiplier`
- cached input price = `effective_input * cache_read_multiplier`

For `claude-sonnet-5`, the effective production prices are:

- input: `300 -> 1200` cents per 1M tokens
- cached input: `30 -> 120` cents per 1M cached-read tokens
- output: `1500 -> 6000` cents per 1M tokens

When changing Claude Code pricing, update all public `claude-*` model overrides together unless there is an explicit SKU-level pricing decision.

## Monitoring Caveat

The provider-channel monitor can fail for Claude Code-only upstreams because it is a server-side probe. A monitor result such as `HTTP 503` does not by itself prove the channel is broken for real Claude Code clients.

Use real request logs as the source of truth for this channel:

- `status_code = 200`
- `channel_id = ch_360294872e2c6ef54b880615`
- `channel_type = anthropic_compatible`
- `provider_platform = sixoner`
- `provider_account_fingerprint = sixoner-claude-code-only`
- `provider_model` equals the requested Claude upstream model
- token fields are populated
- `price_version`, `pricing_mode`, and multiplier fields match the current policy
- `cost_cents` is greater than zero for billable usage

## Runtime Concurrency

Railway runs the API with multiple uvicorn worker processes. HTTP text clients are
per process, so `COINCOIN_HTTP_POOL_MAX` is a per-worker limit, not a deployment
total.

Current repository defaults:

- `WEB_CONCURRENCY`: `2` through the Railway start command default
- `COINCOIN_HTTP_POOL_MAX`: `1000` per worker
- Approximate total outbound text slots: `2 * 1000 = 2000`
- `COINCOIN_HTTP_POOL_KEEPALIVE`: `200` per worker

Keepalive should stay materially lower than max connections so the process does
not retain thousands of idle upstream sockets. Raising the pool only reduces
local gateway queuing; it does not raise upstream account concurrency. If an
upstream has a low account limit, prefer additional healthy routes plus fallback
over continually increasing local connection capacity.

Set the Railway variable explicitly when changing the worker count:

```bash
WEB_CONCURRENCY=2
```

The repository `railway.toml` default is safe for deploys where the variable is
not set, but the Railway environment variable is still the clearest operational
source of truth.

## Claude Messages Fallback

Claude Code traffic enters CoinCoin through `/v1/messages`; routing still uses
the `chat/completions` model-route endpoint because Claude public models share
the same route registry.

Before any bytes are sent to the client, `/v1/messages` should immediately try
the next eligible provider-channel route when the current Claude channel returns
a retryable upstream failure:

- HTTP `429`
- HTTP `502`
- HTTP `503`
- HTTP `504`, including CDN timeout-style failures
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

## Streaming Error Contract

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

CoinCoin also must not record successful usage for a stream that ends in an
error event, even if the upstream had already sent a `message_start` usage block.

Fallback remains a pre-stream decision only. If the upstream fails before any
bytes are sent to the client, CoinCoin may try the next eligible route. If the
upstream fails after the stream is open, CoinCoin returns the sanitized stream
error event and does not splice a second provider's stream into the already-open
response.

## Verification Commands

Check route status:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://clawfather.up.railway.app/admin/model-channel-routes?public_model_id=claude-sonnet-5"
```

Check model pricing:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://clawfather.up.railway.app/admin/model-pricing/claude-sonnet-5"
```

List recent request logs for the user that owns a Claude Code key:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://clawfather.up.railway.app/admin/users/$USER_ID/request-logs?limit=20"
```

Relevant local tests:

```bash
COINCOIN_DATABASE_URL='mysql://127.0.0.1:3306/test' \
  .venv/bin/python -m pytest \
  tests/test_channel_router.py \
  tests/test_anthropic_compat.py \
  tests/test_channel_monitoring.py \
  tests/test_usage_buffer_units.py \
  tests/test_admin_usage_fields.py \
  tests/test_proxy_auth_cache.py -q
```
