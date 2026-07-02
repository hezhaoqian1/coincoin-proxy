# Claude Code Upstream Runbook

Updated: 2026-07-02

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
