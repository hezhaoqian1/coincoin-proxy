# CoinCoin dashboard rolling 24h reporting - Evidence

## RED

- `COINCOIN_DB_HOST=localhost COINCOIN_DB_NAME=test COINCOIN_DB_USER=test COINCOIN_DB_PASSWORD=test PYTHONPATH=. ./.venv/bin/pytest tests/test_admin_usage_fields.py -k rolling_24h -q`
- Initial result: failed with `KeyError: 'period_label'`, proving the new rolling 24h contract was not implemented yet.

## GREEN / Regression

- `COINCOIN_DB_HOST=localhost COINCOIN_DB_NAME=test COINCOIN_DB_USER=test COINCOIN_DB_PASSWORD=test PYTHONPATH=. ./.venv/bin/pytest tests/test_admin_usage_fields.py -k "rolling_24h or revenue_margin_today or top_users_today" -q`
- Result: `3 passed`.
- `PYTHONPATH=. ./.venv/bin/python -m py_compile app/admin.py workers/coincoin-daily-report-bot/report_bot.py`
- Result: exit 0.
- `COINCOIN_DB_HOST=localhost COINCOIN_DB_NAME=test COINCOIN_DB_USER=test COINCOIN_DB_PASSWORD=test PYTHONPATH=. ./.venv/bin/pytest tests/test_admin_usage_fields.py -q`
- Result: `39 passed`.

## Production Pre-Deploy Baseline

- `GET https://clawfather.up.railway.app/admin/analytics/operating-dashboard?period=today`
- Result before push: production still returned no `period_label` / `window_hours`, confirming deploy verification is required after push.
