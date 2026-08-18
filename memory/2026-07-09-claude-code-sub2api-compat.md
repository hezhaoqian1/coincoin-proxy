# Claude Code sub2api compatibility investigation

## Symptom

Customer path `cc-switch -> sub2api -> CoinCoin` failed with:

`HTTP 502: {"error":{"message":"Upstream access forbidden, please contact administrator","type":"upstream_error"},"type":"error"}`

Direct `cc-switch -> CoinCoin` worked.

## Root Cause

The real user request path only passed through Claude Code specific Anthropic headers and query parameters when the inbound request still carried them. An intermediate sub2api can normalize or strip those fields before forwarding to CoinCoin. When CoinCoin then selected a Claude Code-only upstream channel, the final request could miss required Claude Code request shape fields such as `anthropic-beta`, `anthropic-dangerous-direct-browser-access`, `x-app`, Claude CLI `user-agent`, stainless metadata, and `?beta=true`.

The provider-channel monitor already synthesized these fields for Claude Code-only channels, which explained why monitoring could pass while real customer traffic through another gateway could fail.

## Fix

- Added shared Claude Code-only upstream defaults in `app/anthropic_adapter.py`.
- Applied those defaults in both Anthropic native `/v1/messages` forwarding and OpenAI-compatible `/v1/chat/completions` to Anthropic Messages forwarding.
- Kept the behavior gated to channels marked with `cost_tier="claude-code"` or a `provider_account_fingerprint` containing `claude-code`.
- Reused the same helper in channel monitoring to avoid future drift.

## Verification

- `COINCOIN_DATABASE_URL=mysql://127.0.0.1:3306/test .venv/bin/python -m py_compile app/anthropic_adapter.py app/anthropic_compat.py app/openai_compat.py app/channel_monitoring.py tests/test_anthropic_compat.py tests/test_channel_monitoring.py`
- `COINCOIN_DATABASE_URL=mysql://127.0.0.1:3306/test .venv/bin/python -m pytest tests/test_anthropic_compat.py -k "claude_code_defaults or anthropic_compatible_channel" -vv`
- `COINCOIN_DATABASE_URL=mysql://127.0.0.1:3306/test .venv/bin/python -m pytest tests/test_anthropic_compat.py -vv`
- `COINCOIN_DATABASE_URL=mysql://127.0.0.1:3306/test .venv/bin/python -m pytest tests/test_channel_monitoring.py -k "anthropic_compatible_monitor_uses_messages_endpoint or claude" -vv`
- `git diff --check`

Status: DONE

## Follow-up: production top-up and route pinning

After the upstream balance was topped up, the `章鱼Claude MAX 稳定福利`
provider-channel connection test recovered:

- Channel `ch_0515c44d40b02903e8ecd295` `/models`: HTTP 200, `model_count=9`, including `claude-sonnet-5`.

However, real inference through `claude-sonnet-5` still failed while the model was
prioritized to that channel:

- `/v1/messages?beta=true` with Claude Code shaped headers: HTTP 400.
- `/v1/chat/completions` in sub2api/OpenAI compatible shape: HTTP 400.
- The upstream message said the client looked anomalous and asked for a standard
  Claude Code client.
- Adding the full local Claude Code default headers did not change that result.

Sixoner was then tested through existing Sixoner-only routes:

- `/v1/chat/completions` with `model=best`: HTTP 200, returned `pong`.
- `/v1/messages?beta=true` with `model=best`: HTTP 200, returned `pong`.

Production route configuration was updated for `claude-sonnet-5`:

- `mcr_ce06cd5a2479cd76bab3fb5e` / `Sixoner Claude Code`: `priority_override=0`.
- `mcr_be1e6b9a10490929949f32d0` / `章鱼Claude MAX 稳定福利`: `priority_override=10`.

Post-change verification:

- `/v1/chat/completions` with `model=claude-sonnet-5`: HTTP 200, returned `pong`,
  route `catalog:claude-sonnet-5:route_only:channel:ch_360294872e2c6ef54b880615`.
- `/v1/messages?beta=true` with `model=claude-sonnet-5`: HTTP 200, returned `pong`.

Conclusion: after top-up, the remaining failure was not balance. It was a
provider-channel compatibility/client-shape restriction on the 章鱼 channel. For
sub2api and Claude Code relay compatibility, keep `claude-sonnet-5` pinned to
Sixoner primary and keep 章鱼 as a lower-priority backup.
