# CoinCoin Usage and Quota Infrastructure

This document defines the first CoinCoin-only step toward a durable usage,
quota, and ledger platform. The current production owner remains the existing
Python gateway path:

- `app/usage_buffer.py` remains the canonical usage aggregation and DB flush
  owner.
- `app/billing.py` remains the canonical balance, subscription, traffic-pack,
  and billing ledger owner.
- `app/rate_limiter.py` remains the request-limit owner, with optional Redis
  backing when explicitly enabled.

## Phase 1: Shadow Events and Optional Redis Limit

The first implementation intentionally does not replace billing or usage
deduction. It adds:

- `COINCOIN_USAGE_EVENT_SHADOW_ENABLED=false` by default.
- `COINCOIN_REDIS_RATE_LIMITER_ENABLED=false` by default.
- `app/usage_events.py`, a stable usage event envelope for future Go or Python
  ledger writers.
- `app/redis_client.py`, a lazy shared Redis client.
- Redis fixed-window rate limiting behind the existing `RateLimiter.allow`
  interface.

When shadow publishing is enabled, `UsageBuffer.add()` still writes the legacy
in-process buffer first, then schedules a best-effort Redis Stream publish in
the background. Redis failures must never block or break existing requests.

When Redis rate limiting is enabled, `RateLimiter.allow()` uses Redis Lua for
cross-process counters. If Redis is unavailable and
`COINCOIN_REDIS_RATE_LIMITER_FALLBACK_TO_LOCAL=true`, it falls back to the
legacy in-process limiter. If fallback is disabled, Redis failure denies the
request.

## Usage Event Contract

Each event is a Redis Stream record with top-level fields plus a JSON payload:

- `schema_version`
- `event_id`
- `event_type`
- `status`
- `user_id`
- `api_key_id`
- `request_id`
- `reservation_id`
- `created_at`
- `payload`

The `payload` includes normalized `usage`, `cost`, and the legacy request-log
shape. Future ledger writers must treat `event_id` as the idempotency key.

## Compatibility and Retirement

The old path is retained in Phase 1. It should shrink only after:

1. Shadow Redis events are enabled in production.
2. A reconciliation job proves event counts and cost match legacy request logs.
3. A ledger writer is deployed with idempotent writes and retry/dead-letter
   handling.
4. Runtime flags allow rollback without schema or code rollback.

Only then should `usage_buffer.py` stop being the canonical writer.

## Phase 2: Go Usage Quota Service

`usage-quota-service/` is the first Go runtime in CoinCoin Proxy. It is scoped
to usage/quota infrastructure only; it does not replace the Python public API,
model routing, auth, payment, or admin control plane.

The service consumes the Redis Stream produced by `app/usage_events.py`:

```bash
cd usage-quota-service
COINCOIN_REDIS_URL=redis://localhost:6379/0 \
COINCOIN_USAGE_EVENT_STREAM=coincoin:usage:events \
COINCOIN_USAGE_QUOTA_DRY_RUN=true \
go run ./cmd/usage-quota-service
```

Default runtime behavior:

- `COINCOIN_USAGE_QUOTA_DRY_RUN=true`, so no DB mutation happens by default.
- Redis consumer group: `coincoin-usage-quota-service`.
- Dead-letter stream: `<usage-event-stream>:dlq`.
- Health endpoint: `GET /healthz` on `COINCOIN_USAGE_QUOTA_HTTP_ADDR`
  (default `:8091`).
- Metrics endpoint: `GET /metrics`, returning processed, duplicate,
  parse-error, writer-error, DLQ, ack-error, and claimed counters.

The Go service currently provides:

- Redis Stream consumer group creation with `XGROUP CREATE MKSTREAM`.
- Batch `XREADGROUP` consumption.
- Pending message reclaim from other consumers after a configurable idle time.
- Strict usage event payload validation.
- Dry-run ledger writer with idempotency by `event_id`.
- Dry-run shadow summaries in Redis, also idempotent by `event_id`, so replayed
  stream events do not double-count reconciliation totals.
- Retry-by-requeue on writer failures.
- Dead-letter handling for malformed payloads and events that exceed
  `COINCOIN_USAGE_QUOTA_MAX_ATTEMPTS`.
- Redis Lua quota reservations through `POST /v1/quota/reserve`,
  `POST /v1/quota/release`, and `POST /v1/quota/commit`.
- Atomic RPM checks, concurrency slots, and temporary balance reservation
  counters across all gateway instances.

This is intentionally a production-shaped shadow consumer, not the final DB
ledger writer. The Python path remains canonical until reconciliation proves
event parity.

## Phase 3: Shadow Ledger Summary and Reconciliation

The dry-run writer now records operational summaries in Redis while leaving the
Python database ledger untouched. This gives operators a real comparison target
before any future DB writer is allowed to become canonical.

Each accepted event contributes to these rollups:

- day global
- day + user
- day + API key
- day + model
- day + user + API key
- day + user + model
- day + API key + model
- day + user + API key + model

The model dimension is derived from the legacy request-log payload in this
order: `resolved_public_model`, `model`, `customer_model_alias`,
`provider_model`, then `_unknown`.

Counters stored per rollup:

- `events`
- `unit_count`
- `input_tokens`
- `output_tokens`
- `cache_read_tokens`
- `cache_creation_tokens`
- `image_count`
- `video_count`
- `cost_cents`
- `retail_charge_cents`
- `wholesale_cost_cents`

Redis keys use `COINCOIN_REDIS_KEY_PREFIX`, default to a 90 day TTL, and can be
kept longer with `COINCOIN_USAGE_QUOTA_SHADOW_SUMMARY_TTL`.

Query an internal summary:

```http
GET /v1/usage-shadow/summary?day=2026-06-07&user_id=u_123&model=gpt-5.4
```

Response:

```json
{
  "day": "2026-06-07",
  "user_id": "u_123",
  "model": "gpt-5.4",
  "events": 12,
  "unit_count": 82000,
  "input_tokens": 52000,
  "output_tokens": 30000,
  "cache_read_tokens": 0,
  "cache_creation_tokens": 0,
  "image_count": 0,
  "video_count": 0,
  "cost_cents": 170,
  "retail_charge_cents": 170,
  "wholesale_cost_cents": 48
}
```

This endpoint is an internal operational endpoint. Do not expose
`usage-quota-service` directly to public traffic.

### Reconciliation Procedure

For a rollout day:

1. Query Go global shadow totals:

   ```bash
   curl -sS 'http://127.0.0.1:8091/v1/usage-shadow/summary?day=2026-06-07'
   ```

2. Compare against Python `coincoin_request_logs` for the same UTC day:

   ```sql
   SELECT
     COUNT(*) AS events,
     COALESCE(SUM(usage_unit_count), 0) AS unit_count,
     COALESCE(SUM(input_tokens), 0) AS input_tokens,
     COALESCE(SUM(output_tokens), 0) AS output_tokens,
     COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
     COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
     COALESCE(SUM(image_count), 0) AS image_count,
     COALESCE(SUM(video_count), 0) AS video_count,
     COALESCE(SUM(cost_cents), 0) AS cost_cents,
     COALESCE(SUM(retail_charge_cents), 0) AS retail_charge_cents,
     COALESCE(SUM(wholesale_cost_cents), 0) AS wholesale_cost_cents
   FROM coincoin_request_logs
   WHERE created_at >= '2026-06-07 00:00:00'
     AND created_at <  '2026-06-08 00:00:00';
   ```

3. Repeat the comparison for high-traffic users, API keys, and public models.
4. Investigate any mismatch before enabling broader rollout. Common causes are
   disabled shadow publishing on one gateway instance, Redis Stream lag, DLQ
   entries, or request-log rows created before shadow publishing was enabled.
5. Treat `/metrics` and `<stream>:dlq` as part of the evidence bundle. Event
   parity is not proven while parse errors, write errors, or DLQ entries remain
   unexplained.

## Phase 4: Quota Reservation API

The Go service exposes a quota/reservation API for future gateway-side preflight
checks. The intended long-term boundary is:

- Python remains the owner for user identity, API key controls, DB-backed
  subscription/traffic-pack/legacy balance snapshots, model pricing, and final
  request usage events.
- Go owns distributed, low-latency Redis admission control: RPM, concurrency,
  and temporary balance reservations.
- Redis stores only reservations and counters. It is not the source of truth for
  real billing.

### Reserve

```http
POST /v1/quota/reserve
Content-Type: application/json

{
  "user_id": "u_123",
  "api_key_id": "ak_123",
  "station_id": "st_123",
  "channel_id": "ch_123",
  "estimated_cost_cents": 20,
  "available_balance_cents": 1000,
  "rpm_limits": [
    {"dimension": "user", "id": "u_123", "limit": 60, "window_seconds": 60}
  ],
  "concurrency_limits": [
    {"dimension": "user", "id": "u_123", "limit": 8},
    {"dimension": "channel", "id": "ch_123", "limit": 100}
  ],
  "ttl_seconds": 120
}
```

Successful response:

```json
{
  "allowed": true,
  "reservation_id": "qres_...",
  "reason": "reserved",
  "expires_at": "2026-06-07T05:00:00Z"
}
```

Denied responses use:

- `400` for invalid requests.
- `402` for `balance_reserved_exceeded`.
- `429` for RPM, concurrency, or missing reservation denials.
- `500` for internal Redis/service failures.

### Release And Commit

`POST /v1/quota/release` releases concurrency and reserved balance when a
request fails before billable usage is produced.

`POST /v1/quota/commit` marks the reservation as finished after usage is
recorded. In this phase it also releases temporary reservation counters; final
billing remains in Python's usage/ledger path.

Both endpoints accept:

```json
{"reservation_id": "qres_..."}
```

`commit` may also include `actual_cost_cents`; the Redis reservation record
keeps it for reconciliation:

```json
{"reservation_id": "qres_...", "actual_cost_cents": 17}
```

Reservation IDs are idempotency keys. Reusing a finished reservation ID returns
the finished status instead of creating a second reservation.

### Python Gateway Client

`app/quota_client.py` provides the Python-side client. The live gateway hook is
implemented but remains default-off:

- `COINCOIN_QUOTA_RESERVATION_ENABLED=false`
- `COINCOIN_QUOTA_SERVICE_URL=""`
- `COINCOIN_QUOTA_SERVICE_TIMEOUT_SECONDS=0.25`
- `COINCOIN_QUOTA_SERVICE_FAIL_OPEN=true`
- `COINCOIN_QUOTA_USER_CONCURRENCY_LIMIT=0`
- `COINCOIN_QUOTA_API_KEY_CONCURRENCY_LIMIT=0`
- `COINCOIN_QUOTA_STATION_CONCURRENCY_LIMIT=0`

When enabled, `authorize_request()` reserves quota after existing Python auth,
rate, token, key-quota, and balance checks pass. Reservation IDs are attached to
request context and copied into usage events. `UsageBuffer.add()` commits the
reservation with the actual `cost_cents` when billable usage is recorded.
`QuotaReservationASGIMiddleware` releases any reservation that reaches response
completion without recorded usage, including upstream error paths.

Streaming responses use `schedule_usage_add()` instead of a raw background
`asyncio.create_task(usage_buffer.add(...))`. The middleware waits for these
registered usage tasks before releasing, so successful streams can commit
before the fallback release path runs.

This still does not make Go the source of truth for billing. Python remains the
owner for final usage calculation and DB writes; Go owns admission control and
temporary reservation state.

## Runtime Flags

Python gateway flags:

| Variable | Default | Purpose |
| --- | --- | --- |
| `COINCOIN_REDIS_URL` | empty | Shared Redis connection for shadow events and optional Redis limiter. |
| `COINCOIN_USAGE_EVENT_SHADOW_ENABLED` | `false` | Publish usage events to Redis Stream after legacy buffer append. |
| `COINCOIN_USAGE_EVENT_STREAM` | `coincoin:usage:events` | Redis Stream for usage events. |
| `COINCOIN_REDIS_RATE_LIMITER_ENABLED` | `false` | Use Redis Lua fixed-window counters through the existing limiter interface. |
| `COINCOIN_REDIS_RATE_LIMITER_FALLBACK_TO_LOCAL` | `true` | Fall back to process-local limiter if Redis is unavailable. |
| `COINCOIN_QUOTA_RESERVATION_ENABLED` | `false` | Enable default-off live quota reservation hooks in `authorize_request()`. |
| `COINCOIN_QUOTA_SERVICE_URL` | empty | Base URL for `usage-quota-service`, for example `http://127.0.0.1:8091`. |
| `COINCOIN_QUOTA_SERVICE_TIMEOUT_SECONDS` | `0.25` | Timeout for reservation API calls. |
| `COINCOIN_QUOTA_SERVICE_FAIL_OPEN` | `true` | Fail open on quota-service transport errors until rollout proves stability. |
| `COINCOIN_QUOTA_RESERVATION_TTL_SECONDS` | `120` | Reservation TTL passed to the Go quota service. |
| `COINCOIN_QUOTA_USER_CONCURRENCY_LIMIT` | `0` | Optional distributed user concurrency limit. `0` disables this dimension. |
| `COINCOIN_QUOTA_API_KEY_CONCURRENCY_LIMIT` | `0` | Optional distributed API-key concurrency limit. `0` disables this dimension. |
| `COINCOIN_QUOTA_STATION_CONCURRENCY_LIMIT` | `0` | Optional distributed station concurrency limit. `0` disables this dimension. |

Go service flags:

| Variable | Default | Purpose |
| --- | --- | --- |
| `COINCOIN_REDIS_URL` | empty | Required Redis URL. |
| `COINCOIN_REDIS_KEY_PREFIX` | `coincoin` | Prefix for quota reservation Redis keys. |
| `COINCOIN_USAGE_EVENT_STREAM` | `coincoin:usage:events` | Source Redis Stream. |
| `COINCOIN_USAGE_QUOTA_GROUP` | `coincoin-usage-quota-service` | Redis consumer group. |
| `COINCOIN_USAGE_QUOTA_CONSUMER` | hostname | Consumer name. |
| `COINCOIN_USAGE_QUOTA_DLQ_STREAM` | `<stream>:dlq` | Dead-letter Stream. |
| `COINCOIN_USAGE_QUOTA_BATCH_SIZE` | `100` | `XREADGROUP` batch size. |
| `COINCOIN_USAGE_QUOTA_BLOCK_TIMEOUT` | `5s` | Blocking read timeout. |
| `COINCOIN_USAGE_QUOTA_RECLAIM_MIN_IDLE` | `2m` | Pending message reclaim threshold. |
| `COINCOIN_USAGE_QUOTA_MAX_ATTEMPTS` | `5` | Max writer attempts before DLQ. |
| `COINCOIN_USAGE_QUOTA_DRY_RUN` | `true` | Keep writer non-mutating; `false` fails fast until the DB ledger writer ships. |
| `COINCOIN_USAGE_QUOTA_HTTP_ADDR` | `:8091` | Health/metrics listen address. |
| `COINCOIN_USAGE_QUOTA_DATABASE_DSN` | empty | Reserved for the future DB ledger writer. |
| `COINCOIN_USAGE_QUOTA_SHADOW_SUMMARY_TTL` | `2160h` | Redis retention for dry-run shadow summary and event idempotency keys. |

## Rollout

1. Deploy Redis and set `COINCOIN_REDIS_URL`.
2. Enable `COINCOIN_USAGE_EVENT_SHADOW_ENABLED=true` on one gateway instance.
3. Run `usage-quota-service` with `COINCOIN_USAGE_QUOTA_DRY_RUN=true`.
4. Watch `/metrics`, Redis Stream lag, and `<stream>:dlq`.
5. Compare Go dry-run event counts/cost from `/v1/usage-shadow/summary` with
   legacy `RequestLog` and `UsageDaily` aggregates.
6. Enable shadow events on all gateway instances.
7. Exercise `POST /v1/quota/reserve` / `release` / `commit` from staging with
   synthetic traffic and verify Redis keys expire after TTL.
8. Enable `COINCOIN_QUOTA_RESERVATION_ENABLED=true` in staging with small
   concurrency limits and verify successful requests commit while upstream
   failures release.
9. Canary one production gateway instance with fail-open enabled.
10. Only after parity is proven, implement and enable the DB ledger writer behind
   a new explicit flag.

## Rollback

- Set `COINCOIN_USAGE_EVENT_SHADOW_ENABLED=false` to stop publishing events.
- Stop `usage-quota-service`; Python usage buffer and billing continue to run.
- Set `COINCOIN_REDIS_RATE_LIMITER_ENABLED=false` to return to the process-local
  limiter.
- Set `COINCOIN_QUOTA_RESERVATION_ENABLED=false` to bypass the Go reservation
  client when it is later wired into request lifecycle.
- Keep the Redis Stream for forensic reconciliation; do not delete it during an
  incident unless storage pressure requires a separate retention action.
