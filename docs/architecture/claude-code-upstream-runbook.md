# Claude Code Upstream Runbook

Updated: 2026-07-22

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

## Runtime Fallback and Failure Records

Both non-streaming and streaming `POST /v1/messages` requests immediately try the next active provider-channel route when the current channel returns `429`, `502`, or `503`, or when the initial upstream connection times out or fails. The retry keeps the public CoinCoin model id and changes only the selected channel and provider model. A stream is never replayed after response events have already been emitted because replaying partial output can duplicate text or tool calls.

Every failed upstream attempt is written to `coincoin_request_logs`, including attempts with no token usage. Buffered log rows receive stable ids before the database flush, are retried after transient database failures, and use idempotent inserts so an ambiguous commit cannot create duplicates. Failed attempts have zero token usage and zero retail/wholesale charge. Intermediate failed attempts do not increment aggregate request totals; the final success or terminal failure increments the logical request exactly once. Streaming requests use endpoint `messages:stream`; non-streaming requests use `messages`.

Terminal upstream failures return a short CoinCoin error rather than the provider or Cloudflare response body. The response body and `request-id` header include a generated `ccreq_*` id. The same id is stored at the start of `upstream_request_id` in the failed RequestLog so support can trace the client-visible error without exposing the upstream hostname, Cloudflare Ray id, API key, or raw HTML.

## User-path Upstream Failure Alerts

The gateway counts only failures observed while handling authenticated user `/v1/messages` traffic. Provider discovery, channel monitors, health probes, and admin connection tests call their own upstream paths and do not enter these counters.

Failures are grouped per channel and endpoint into availability (`5xx` and connection errors), capacity (`429`), and authentication (`401`/`403`) categories. The default policy alerts after 5 availability/capacity failures or 3 authentication failures in a rolling 60-second window, then deduplicates that category key for 300 seconds. Configure it with:

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

For `claude-sonnet-5`, the effective production prices are:

- input: `300 -> 1800` cents per 1M tokens
- cached input: `30 -> 180` cents per 1M cached-read tokens
- output: `1500 -> 9000` cents per 1M tokens

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
