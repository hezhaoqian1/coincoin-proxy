# Usage and Quota Go Optimization Report

Date: 2026-06-07

Scope: CoinCoin Proxy gateway only.

## Summary

This change introduces a long-term Go/Redis foundation for two high-value
gateway bottlenecks:

1. Usage event collection and future ledger writing.
2. Distributed rate limiting and quota reservations.

The current Python request path remains the production source of truth. The Go
service is intentionally deployed as a shadow/dry-run sidecar until event parity
and reservation lifecycle coverage are proven.

## Why This Optimization Is Worth Doing

The original Python gateway already works, but two areas become fragile as the
business scales:

- `app/usage_buffer.py` is an in-process, lock-sharded buffer. It is fast for a
  single process, but it cannot be the durable coordination point across many
  workers or instances.
- `app/rate_limiter.py` was process-local. Multiple workers could each admit
  traffic independently, so the configured limit was not a true global limit.

The Go/Redis direction is a good long-term optimization because Redis Streams
and Redis Lua give us durable queues, atomic counters, consumer groups, retries,
DLQ handling, and cross-instance coordination without forcing every request to
wait on heavy DB writes.

## What Changed

### Python Gateway

- Added optional Redis configuration in `app/config.py`.
- Added `app/redis_client.py` for a lazy shared async Redis client.
- Added `app/usage_events.py` for a stable usage event envelope.
- Updated `UsageBuffer.add()` to publish a best-effort shadow usage event after
  the legacy buffer append.
- Updated `app/rate_limiter.py` with optional Redis Lua fixed-window limits.
- Added `app/quota_client.py` and `app/quota_lifecycle.py` for default-off Go
  quota reservations.
- Added request lifecycle hooks:
  - `authorize_request()` reserves after existing Python checks pass.
  - `UsageBuffer.add()` commits reservations with actual request cost.
  - `QuotaReservationASGIMiddleware` releases reservations that complete without
    usage.
  - streaming usage tasks are registered through `schedule_usage_add()` so the
    middleware waits for commit before fallback release.
- Closed the shared Redis client during FastAPI shutdown.

All Python-side features are default-off unless explicitly enabled.

### Go Usage Quota Service

Added `usage-quota-service/`, a Go module that provides:

- Redis Stream consumer group setup.
- `XREADGROUP` batch consumption.
- pending event reclaim.
- retry-by-requeue on writer failure.
- dead-letter stream for malformed events and max-attempt failures.
- strict usage event schema validation.
- dry-run ledger writer.
- idempotent shadow usage summaries in Redis.
- HTTP health and metrics endpoints.
- quota reservation API:
  - `POST /v1/quota/reserve`
  - `POST /v1/quota/release`
  - `POST /v1/quota/commit`

### Shadow Ledger Summary

The Go dry-run writer now records Redis summaries that can be compared against
Python `coincoin_request_logs` and `coincoin_usage_daily`.

The Redis write is atomic:

- `SET NX` records the `event_id` idempotency key.
- If the event was already seen, no counters are incremented.
- If it is new, a Lua script increments all summary rollups together.

Supported rollups:

- global day
- day + user
- day + API key
- day + model
- day + user + API key
- day + user + model
- day + API key + model
- day + user + API key + model

Query endpoint:

```http
GET /v1/usage-shadow/summary?day=2026-06-07&user_id=u_123&model=gpt-5.4
```

## What Did Not Change

These production owners are intentionally retained:

- `app/usage_buffer.py` remains the canonical usage aggregation and DB flush
  path.
- `app/billing.py` remains the canonical balance, subscription, traffic-pack,
  referral, and finance-summary owner.
- Request logs still use the current Python DB write path.
- Quota reservation hooks remain disabled by default and require
  `COINCOIN_QUOTA_RESERVATION_ENABLED=true`, `COINCOIN_QUOTA_SERVICE_URL`, and
  at least one active distributed limit or estimated cost.
- `COINCOIN_USAGE_QUOTA_DRY_RUN=false` is rejected until a complete DB ledger
  writer ships.

This keeps production billing stable while allowing the Go reservation lifecycle
to be exercised in staging and canary before becoming a hard dependency.

## Safety Properties

- Default-off Python shadow publishing.
- Default-off Redis rate limiting.
- Default-off quota reservation lifecycle.
- Redis limiter can fall back to local limiter.
- Quota service transport failures can fail open during rollout.
- Usage shadow publish is best effort and does not block request completion.
- Reservation IDs flow into shadow usage events for reconciliation.
- Go stream consumer has retries, DLQ, and pending reclaim.
- Go summary writer is idempotent by `event_id`.
- Go non-dry-run mode fails fast instead of silently mutating billing data.

## Verification

Fresh checks run from `coincoin-proxy`:

```bash
go test -vet=off -count=1 -timeout=60s -v ./...
```

Run from `coincoin-proxy/usage-quota-service`.

Result: all Go packages passed.

```bash
env PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/pycache \
  COINCOIN_DB_HOST=localhost \
  COINCOIN_DB_NAME=test \
  COINCOIN_DB_USER=test \
  COINCOIN_DB_PASSWORD=test \
  .venv312/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Result: `Ran 332 tests in 9.167s OK (skipped=1)`.

```bash
git diff --check
```

Result: passed with no whitespace errors.

## Review Notes

The main compatibility risk was accidental behavioral change in the hot request
path. The implementation avoids that by publishing shadow events only after the
legacy request-log payload is appended to the in-process buffer. If Redis is
down, the background publish logs an error and the legacy buffer remains intact.

The main Go migration risk was double-counting during stream retry or pending
reclaim. The summary store handles this with Redis `SET NX` idempotency before
counter increments.

The main quota risk was streaming lifecycle coverage. The implementation now
routes streaming usage writes through `schedule_usage_add()` and makes the
middleware wait for registered usage tasks before it releases a reservation.
That keeps successful streams on the commit path and upstream failures on the
release path.

## Rollout Recommendation

1. Deploy Redis.
2. Run `usage-quota-service` with `COINCOIN_USAGE_QUOTA_DRY_RUN=true`.
3. Enable `COINCOIN_USAGE_EVENT_SHADOW_ENABLED=true` on one gateway instance.
4. Monitor `/metrics`, Redis Stream lag, and the DLQ stream.
5. Compare `/v1/usage-shadow/summary` with Python request-log SQL aggregates.
6. Enable shadow publishing on all gateway instances after parity.
7. Enable quota reservations in staging with small distributed concurrency
   limits and fail-open enabled.
8. Canary one production gateway instance after staging proves reserve,
   commit, and release behavior.
9. Only after reservation lifecycle and usage parity are proven, implement the
   real DB ledger writer behind a new explicit flag.

## Residual Risk

- No live Redis integration test was run in this pass; Redis Lua behavior is
  covered by unit-level contracts and code review, but not by a real Redis
  container in CI.
- The Go DB ledger writer is not implemented yet. That is deliberate because
  copying `billing.py` side effects without full parity would be riskier than
  staying in shadow mode.
- Quota reservations are wired but default-off. They should stay fail-open
  during the first staging and production canary because Redis/service outages
  would otherwise become request-path outages.
