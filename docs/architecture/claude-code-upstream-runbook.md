# Claude Code Upstream Runbook

Updated: 2026-08-10

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

## Customer One-click Configuration

The hosted Claude Code guide preserves existing `~/.claude/settings.json` fields and updates only `env.ANTHROPIC_BASE_URL` and `env.ANTHROPIC_AUTH_TOKEN`. On macOS and Linux, the installer first proves that a Python 3.8+ interpreter can execute, writes the settings atomically with mode `0600`, then reads the file back and verifies both values before launching `claude`. A broken interpreter that exits successfully without running the script therefore stops configuration instead of silently starting Claude with stale settings.

If the command reports a verification error, use the generated backup beside `settings.json` to recover and correct the local Python installation before retrying. Restart any Claude Code process that was already running because an existing process does not reload changed environment settings.

## Current Sixoner Claude Code Channel

Production channel:

- Channel id: `ch_360294872e2c6ef54b880615`
- Channel name: `Sixoner Claude Code`
- Base URL: `https://sub.sixoner.com`
- Channel status: `active`

The channel is intended for Claude Code traffic. Ordinary OpenAI-compatible requests or generic server-side probes can be rejected by the upstream's edge controls even when real Claude Code traffic works.

## Public Claude Models

The Claude Code family is exposed through public `claude-*` model ids. Current generation models include:

- `claude-haiku-4.5`
- `claude-haiku-4-5`
- `claude-opus-4.5`
- `claude-opus-4.6`
- `claude-opus-4-6`
- `claude-opus-4.7`
- `claude-opus-4.8`
- `claude-opus-5`
- `claude-sonnet-4`
- `claude-sonnet-4.5`
- `claude-sonnet-4.6`
- `claude-sonnet-5`

Model version suffixes are provider model identifiers, not a global Claude naming
rule. In particular, Sixoner and 86 advertise Haiku 4.5 as
`claude-haiku-4-5-20251001`. Do not change Sonnet or Opus routes to end in `1001`;
use each provider's exact advertised identifier.

`claude-opus-5` must use the exact upstream model id and an Anthropic Messages transform:

- `public_model_id`: `claude-opus-5`
- `endpoint`: `chat/completions`
- `upstream_model`: `claude-opus-5`
- `transform_profile`: `anthropic_messages`
- `status`: `active`

Provider-route aliases are declared in `config/model_catalog.json` under
`metadata.provider_route_aliases`. The router checks exact provider-channel
routes first. If none exist, it checks aliases in order. `claude-opus-4-6`
therefore reuses `claude-opus-4.6` routes on every configured Claude upstream
unless an operator creates explicit `claude-opus-4-6` routes.

As of 2026-08-04, the verified non-1M 86 mappings used for fallback are:

| Public model or alias | 86 upstream model |
| --- | --- |
| `haiku`, `claude-haiku-4.5`, `claude-haiku-4-5` | `claude-haiku-4-5-20251001` |
| `sonnet`, `claude-sonnet-4`, `claude-sonnet-4.5`, `claude-sonnet-4.6` | `claude-sonnet-4-6` |
| `opus`, `opusplan`, `best`, `default` | `claude-opus-4-7` |
| `claude-opus-4.5` | `claude-opus-4-5-20251101` |
| `claude-opus-4.6`, `claude-opus-4-6` | `claude-opus-4-6` |
| `claude-opus-5` | `claude-opus-5` |

Do not add `opus[1m]` or `sonnet[1m]` routes to 86 until a direct 1M-context
request succeeds. Advertising a base model does not prove support for an extended
context alias.

Claude public models should remain route-only for Claude Code upstream coverage. Do not silently fall back to GPT-backed Claude aliases for these models.

## Route Order And Concurrency

Claude fallback order must be deterministic:

1. Sixoner Claude Code, effective priority `0`.
2. 86 CLAUDE, effective priority `1`.
3. Zhangyu Claude MAX, effective priority `2`.

Route-level `priority_override` values take precedence over the channel priority.
Leave the override empty when the route should inherit the channel's order. Two
fallback channels at the same priority are weight-selected, not ordered.

86 currently allows five concurrent requests. The gateway does not reserve a
separate local semaphore for this provider. When 86 is saturated it can return 429;
429 is retryable and therefore continues to Zhangyu when that route exists. Keep the
third route active for busy models and watch the capacity alert category rather than
assuming five upstream slots equal five CoinCoin users.

## Runtime Fallback and Failure Records

Both non-streaming and streaming `POST /v1/messages` requests immediately try the
next active provider-channel route when the current channel returns 408, 429, any
5xx status, or when the initial upstream connection times out or fails. This includes
Cloudflare 524, which means Cloudflare connected to the origin but did not receive a
response before its origin timeout. The retry keeps the public CoinCoin model id and
changes only the selected channel and provider model. A stream is never replayed
after response events have already been emitted because replaying partial output can
duplicate text or tool calls.

Do not automatically retry ordinary 400, 404, or 422 responses. A 404 model error is
usually a bad route identifier and repeating it on unrelated channels can hide a
configuration mistake. Correct the route using the procedure below.

Every failed upstream attempt is written to `coincoin_request_logs`, including
attempts with no token usage. A terminal local HTTP connection-pool timeout is also
logged with status 503, but it does not mark the selected provider unhealthy or enter
the upstream alert counter because no upstream request was sent. Buffered log rows
receive stable ids before the database flush, are retried after transient database
failures, and use idempotent inserts so an ambiguous commit cannot create duplicates.
Failed attempts have zero token usage and zero retail/wholesale charge. Intermediate
failed attempts do not increment aggregate request totals; the final success or
terminal failure increments the logical request exactly once. Streaming requests use
endpoint `messages:stream`; non-streaming requests use `messages`.

## Route Model Audit And 404 Repair

In the administrator provider-channel list, open **Upstream Models** for a channel.
CoinCoin compares the complete advertised `/models` response with every active route
for that channel. The summary reports one of:

- `路由审计通过`: every checked active route model is advertised.
- `路由警告`: one or more active route model ids are absent; edit or disable each
  listed route before treating the channel as healthy for that public model.
- `路由审计不可用`: the upstream model list failed, was empty, or only a single
  Messages probe succeeded. This is not proof that the routes are invalid.

The audit is advisory and never changes production routes automatically. To repair a
model 404:

1. Read the failed RequestLog's `channel_id`, public `model`, `provider_model`, and
   request id.
2. Open that channel's **Upstream Models** view and confirm the exact advertised id.
3. Patch the existing route rather than adding a duplicate route at the same priority.
4. Refresh the router, re-open **Upstream Models**, and require the warning to clear.
5. Send one direct upstream request and one CoinCoin `/v1/messages` request for the
   public alias; confirm the successful RequestLog uses the intended channel/model.

Terminal upstream failures return a short CoinCoin error rather than the provider or Cloudflare response body. The response body and `request-id` header include a generated `ccreq_*` id. The same id is stored at the start of `upstream_request_id` in the failed RequestLog so support can trace the client-visible error without exposing the upstream hostname, Cloudflare Ray id, API key, or raw HTML.

## User-path Upstream Failure Alerts

The gateway counts only failures observed while handling authenticated user `/v1/messages` traffic. Provider discovery, channel monitors, health probes, and admin connection tests call their own upstream paths and do not enter these counters.

Failures are grouped per channel and endpoint into availability (`408`, `5xx`, and
connection errors), capacity (`429`), and authentication (`401`/`403`) categories.
The default policy alerts after 5 availability/capacity failures or 3 authentication
failures in a rolling 60-second window, then deduplicates that category key for 300
seconds. Configure it with:

```bash
COINCOIN_FALLBACK_ALERT_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=...
COINCOIN_FALLBACK_ALERT_KEYWORD=CoinCoinAlert
COINCOIN_FALLBACK_ALERT_ENABLED=true
COINCOIN_FALLBACK_ALERT_MAX_PENDING_TASKS=256
COINCOIN_UPSTREAM_FAILURE_ALERT_THRESHOLD=5
COINCOIN_UPSTREAM_AUTH_ALERT_THRESHOLD=3
COINCOIN_UPSTREAM_FAILURE_ALERT_WINDOW_SECONDS=60
COINCOIN_UPSTREAM_FAILURE_ALERT_DEDUP_SECONDS=300
```

The environment variables are safe startup defaults. An administrator can view, replace, or clear the full DingTalk webhook and change the enable switch, availability/rate-limit threshold, authentication threshold, rolling window, deduplication period, and maximum pending task count in the existing **Service Reliability** page. The complete validated policy and webhook are stored in plaintext in `coincoin_system_settings`, apply immediately on the current replica, and propagate to other replicas through the existing runtime-settings refresh loop. A present `fallback_alert_webhook_url` database key is authoritative, including an empty value that explicitly disables delivery; `COINCOIN_FALLBACK_ALERT_WEBHOOK_URL` is consulted only while that key is absent. Access to the database and protected admin config API therefore grants access to the webhook credential.

The same page can send one clearly labelled `配置测试` message and lists the latest 50 delivery attempts by default, with category/status filters and a hard API limit of 100. `coincoin_alert_events` records only actual DingTalk delivery attempts (`pending`, `sent`, or `failed`) and sanitized response status/error summaries. It never stores the webhook, API keys, upstream/Cloudflare response bodies, or raw DingTalk response bodies. `coincoin_request_logs` remains the source of truth for each upstream failure, including failures suppressed by burst deduplication.

When `COINCOIN_REDIS_URL` is configured, the rolling counter and deduplication are shared across replicas. Without Redis, the gateway uses a process-local counter; this remains non-blocking but each replica counts independently. The customer request coroutine only performs bounded in-memory checks and schedules a tracked task. That same tracked task counts the failure and directly delivers a threshold alert, so the configured task cap cannot starve a nested sender. Redis counting, `AlertEvent` writes, and DingTalk delivery all run inside controlled background tasks, bounded by `COINCOIN_FALLBACK_ALERT_MAX_PENDING_TASKS`; each best-effort audit write is capped at 250 ms so a slow database cannot suppress the DingTalk request. In-flight tasks are drained during graceful shutdown. Alert persistence or delivery failures never fail or delay the user response.

## Pricing Multiplier Policy

Claude Code public models use model-level pricing overrides in `/admin/model-pricing/{model_id}`.

Current production policy:

- `model_multiplier`: `6.0`
- `output_multiplier`: `1.0`
- `cache_read_multiplier`: `0.1`
- `pricing_mode`: `multiplier`

The router computes effective prices as:

- input price = `base_input * model_multiplier`
- output price = `base_output * model_multiplier * output_multiplier`
- cached input price = `effective_input * cache_read_multiplier`
- cache creation price = `effective_input * cache_creation_multiplier`

The checked-in base price for `claude-opus-5` matches Anthropic's standard API price:

- input: `500` cents per 1M tokens
- 5-minute cache write: `625` cents per 1M cache-creation tokens (`1.25x`)
- cache hit: `50` cents per 1M cache-read tokens (`0.1x`)
- output: `2500` cents per 1M tokens

With the current `6.0x` production model multiplier, the effective retail prices are `3000` input, `3750` cache creation, `300` cache read, and `15000` output cents per 1M tokens.

For `claude-sonnet-5`, the effective production prices are:

- input: `300 -> 1800` cents per 1M tokens
- cached input: `30 -> 180` cents per 1M cached-read tokens
- output: `1500 -> 9000` cents per 1M tokens

When changing Claude Code pricing, update all public `claude-*` model overrides together unless there is an explicit SKU-level pricing decision.

## Monitoring Caveat

The provider-channel monitor can fail for Claude Code-only upstreams because it is a server-side probe. A monitor result such as `HTTP 503` does not by itself prove the channel is broken for real Claude Code clients.

Text-generation probes allow up to 64 output tokens. A valid Anthropic `thinking`/`redacted_thinking`, OpenAI Responses `reasoning`, or Chat Completions `reasoning_content` result that explicitly stops because this probe budget was exhausted is recorded as `degraded: probe output truncated before visible text`, not as a failed empty response. A 2xx response that completes without assistant text and without this explicit token-exhaustion evidence remains `failed: response missing structured model output`.

The same response-shape contract is shared by Claude and ChatGPT-compatible representative probes. This channel-health contract is separate from the DingTalk burst counter described above, which currently counts authenticated user `/v1/messages` failures only.

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

## Verification Commands

Check route status:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://clawfather.up.railway.app/admin/model-channel-routes?public_model_id=claude-opus-5"
```

Audit all active routes against one channel's advertised models:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://clawfather.up.railway.app/admin/provider-channels/$CHANNEL_ID/upstream-models"
```

Check model pricing:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://clawfather.up.railway.app/admin/model-pricing/claude-opus-5"
```

Check the active alert policy and complete webhook through the protected, non-cacheable admin response:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://coincoin.ai/admin/alerts/config"
```

List recent DingTalk delivery attempts:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://coincoin.ai/admin/alerts/events?limit=50"
```

List recent request logs for the user that owns a Claude Code key:

```bash
curl -fsS -H "Authorization: Bearer $COINCOIN_ADMIN_TOKEN" \
  "https://clawfather.up.railway.app/admin/users/$USER_ID/request-logs?limit=20"
```

Relevant local tests:

```bash
COINCOIN_DATABASE_URL='mysql://test:test@127.0.0.1:3306/test' \
  .venv/bin/python -m pytest \
  tests/test_channel_router.py \
  tests/test_anthropic_compat.py \
  tests/test_channel_monitoring.py \
  tests/test_usage_buffer_units.py \
  tests/test_admin_usage_fields.py \
  tests/test_proxy_auth_cache.py -q
```
