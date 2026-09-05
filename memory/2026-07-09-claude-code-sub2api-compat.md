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

- `COINCOIN_DATABASE_URL=mysql://test:test@127.0.0.1:3306/test .venv/bin/python -m py_compile app/anthropic_adapter.py app/anthropic_compat.py app/openai_compat.py app/channel_monitoring.py tests/test_anthropic_compat.py tests/test_channel_monitoring.py`
- `COINCOIN_DATABASE_URL=mysql://test:test@127.0.0.1:3306/test .venv/bin/python -m pytest tests/test_anthropic_compat.py -k "claude_code_defaults or anthropic_compatible_channel" -vv`
- `COINCOIN_DATABASE_URL=mysql://test:test@127.0.0.1:3306/test .venv/bin/python -m pytest tests/test_anthropic_compat.py -vv`
- `COINCOIN_DATABASE_URL=mysql://test:test@127.0.0.1:3306/test .venv/bin/python -m pytest tests/test_channel_monitoring.py -k "anthropic_compatible_monitor_uses_messages_endpoint or claude" -vv`
- `git diff --check`

Status: DONE
