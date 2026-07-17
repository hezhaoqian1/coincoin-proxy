# CoinCoin Admin Performance Optimization Brief

## Status

- Date: 2026-07-17
- Status: approved by user in conversation
- Change kind: performance repair and internal query-path retirement

## Task Intent

Reduce intermittent admin-console stalls without changing gateway forwarding,
billing debit, payment, routing, or public OpenAI-compatible behavior.

Success evidence:

1. The admin user list loads billing state in a bounded number of SQL calls
   instead of three calls per returned user.
2. The dashboard loads the 1h, 4h, and 24h leaderboards from one aggregation
   request and one database query.
3. Operating-dashboard cold recomputation is single-flight and cached for a
   longer bounded interval; payload fields remain compatible.
4. Provider-channel recent usage queries are time-bounded, while historical
   totals are cached and never recomputed for every page open.
5. Admin responses expose server processing time and slow admin requests are
   logged without recording tokens, credentials, bodies, or customer data.

## Baseline and Authority

- Runtime truth: `app/admin.py`, `app/billing.py`, `app/main.py`, and
  `app/static/admin.html`.
- Regression truth: `tests/test_admin_usage_fields.py` and the closest main-app
  tests.
- Existing dashboard contract and compatibility boundary:
  `docs/aegis/plans/2026-05-13-operating-dashboard-v2.md`.
- Repository rules: preserve existing admin endpoint response fields and do not
  mutate production state or add a database migration in this slice.

Formal baseline snapshots are currently absent. The code, executable tests,
repository instructions, and existing dashboard plan are sufficient authority
for this bounded performance repair.

## Options Considered

### 1. Add persistent pre-aggregation tables

Best long-term query efficiency, but requires schema, migration, backfill,
rollout, and retirement planning. Rejected for this first repair because it is
the highest-risk path and is not necessary to remove the worst query fan-out.

### 2. Batch, bound, combine, and cache existing read paths

Recommended. Batch billing reads at the existing admin list owner, page the UI, combine
leaderboards into one aggregation, protect analytics cold cache, bound recent
channel scans, cache historical totals, and add timing evidence. This produces
large gains without changing persistent state.

### 3. Frontend-only throttling or loading indicators

Rejected. It would hide the symptom while leaving database pressure and slow
queries intact.

## Approved Design

### User and finance lists

`app/admin.py` retains the existing `_admin_billing_states_batch` owner from the
latest master branch. The optimized path performs one subscription query, one
active-pack query, MySQL-compatible chunked UNION queries that return at most
50 history rows per user, and one permanent-credit query. It removes the
MySQL-version-sensitive window query without pulling unbounded history into
Python, then uses the canonical serializer in `app/billing.py`. User and
finance lists use this owner; detail and mutation responses retain the existing
single-user function.

The admin UI requests 50 users per page. Existing callers that omit pagination
remain compatible with the current 200-row default.

### Dashboard leaderboards

A new admin-only batch endpoint returns the existing leaderboard item shape for
1h, 4h, and 24h. One query scans the last 24 hours and calculates conditional
aggregates for all three windows. The old single-window endpoint remains as a
compatibility exception, but the admin UI no longer calls it three times.

### Operating analytics

The existing payload remains canonical. Cache TTL increases from 60 seconds to
300 seconds. A per-period `asyncio.Lock` ensures concurrent cold requests do not
run duplicate 30-query recomputations. Billing, payment, or routing data is not
served from this cache.

### Provider-channel usage

Recent 1h, 4h, and current-day values use a query bounded by the current-day
start. Historical totals use the same request-log owner but are cached for 15
minutes. No database schema or source-of-truth ownership changes.

### Observability

An HTTP middleware times `/admin/` requests, adds `Server-Timing` and
`X-Process-Time-Ms`, and logs requests slower than one second. Logs contain only
method, path, status, and duration.

## Compatibility Boundary

- No changes to charge calculation, debit ordering, subscriptions, traffic-pack
  consumption, payment confirmation, public API routing, or database schema.
- Existing admin endpoint fields remain available.
- Existing `/admin/usage/leaderboard` remains supported.
- Cached statistics may be up to 5 minutes old; provider historical totals may
  be up to 15 minutes old. Current billing state is never cached by this work.

## Performance Budgets

- `/admin/users` without API-key lookup: at most three SQL executions for the
  page data and billing state, independent of returned user count.
- Admin UI user page: 50 rows per request.
- Three dashboard leaderboard windows: one HTTP request and one SQL execution.
- Concurrent cold operating-dashboard requests for the same period: one payload
  build.
- Warm provider-channel page: no all-history request-log aggregation.

## Testing

- Query-count tests with the existing fake async DB.
- Response-shape tests for batch billing and batch leaderboard payloads.
- Cache hit and concurrent cold-start tests.
- Middleware header and slow-log tests.
- Existing admin regression module and frontend build.

## Retirement

- Retire per-user billing calls from list endpoints immediately.
- Retire the admin UI's three calls to the single-window leaderboard endpoint.
- Keep the old endpoint only for external compatibility; delete it only after
  usage evidence shows no external callers.
- Retire per-page all-history provider aggregation; retain a bounded cached
  compatibility calculation until a future persistent rollup is explicitly
  approved.
