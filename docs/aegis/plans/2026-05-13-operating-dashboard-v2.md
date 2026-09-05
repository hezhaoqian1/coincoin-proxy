# CoinCoin Operating Dashboard v2 Plan

## Goal
Build a useful company operating cockpit, not a KPI dump. The dashboard and daily report must answer growth, revenue/margin, usage structure, channel health, and today action items with owner/evidence/suggested action.

## Architecture
`app/admin.py` owns the aggregation API. `app/static/admin.html` consumes the same API for the admin dashboard. `workers/coincoin-daily-report-bot/report_bot.py` renders the daily PNG from the same operating-dashboard payload. This keeps dashboard and image report on one data contract.

## Tech Stack
FastAPI, SQLAlchemy async MySQL, static admin HTML/JS, Pillow worker PNG renderer, Railway scheduled worker.

## Baseline/Authority Refs
- Slock task #4 in #coincoin数据.
- Cindy product acceptance: first screen judgement, 3-5 actions, missing fields explain affected decisions, one aggregation API for dashboard and image.
- Eric constraints: repo under `~/code`, report must be useful for company decisions, dashboard can be built today.

## Compatibility Boundary
Existing `/admin/analytics/overview`, `/top-users`, `/low-balance-users`, and `/errors` stay compatible. New v2 endpoints add fields and do not change public API gateway behavior. Worker cron remains `0 0 * * *` UTC.

## Verification
- Python compile: `COINCOIN_DATABASE_URL=... .venv/bin/python -m py_compile app/admin.py app/main.py workers/coincoin-daily-report-bot/report_bot.py`
- Unit tests: `COINCOIN_DATABASE_URL=... .venv/bin/python -m pytest tests/test_admin_usage_fields.py`
- Real DB smoke: call `analytics_operating_dashboard(period='today')` against real MySQL and record cold/cache timing and action count.
- Rendering smoke: `python workers/coincoin-daily-report-bot/report_bot.py --input-json /tmp/coincoin-v2-dashboard-data.json --output /tmp/coincoin-v2-operating-dashboard.png --dry-run-label`
- Production API smoke after main API deployment: `GET /admin/analytics/operating-dashboard?period=today` returns JSON and cached open target is under 2s.

## Implementation Tasks
1. Add v2 aggregation endpoints in `app/admin.py`: growth, revenue-margin, usage-structure, channel-health, action-items, operating-dashboard.
2. Add dashboard cache metadata so humans can see generated time and cache freshness.
3. Update `app/static/admin.html` to show judgement, actions, key metrics, growth, revenue/margin, usage, channel health, risk, and error sections.
4. Update worker renderer to consume the same operating-dashboard payload and support local JSON snapshot rendering for product review while main API deploy is blocked.
5. Verify locally with tests, real DB smoke, and a rendered v2 PNG.
6. Deploy main API when Railway project/service is available; worker deployment already follows repo commit.

## Risks
- Main API Railway project/service is not currently visible under the available token, so production API deployment may require @user-user-020b to grant project access or provide service ID.
- True gross margin remains unavailable until request logs get reliable upstream cost. The v2 UI must show that this blocks profit judgment, not fake margin.
- Channel type is inferred from route_reason until backend stores stable `channel_type`.

## Retirement
v1 daily image remains a fallback only until v2 production API and image are accepted. Once v2 is accepted and cron is stable, the old KPI-only layout should be retired from the worker path.
