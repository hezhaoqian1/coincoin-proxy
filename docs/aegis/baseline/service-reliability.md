# Service Reliability Baseline

Status: `current-state-snapshot`
Date: `2026-07-14`
Decision: `docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md`

## Ownership Map

- `ModelChannelRoute` is the source of truth for configured delivery paths and supported text monitor targets.
- `app/channel_monitoring.py` owns background active probes, retained monitor history, daily rollups, and route-derived monitor reconciliation.
- `app/reliability.py` owns the bounded admin read model and its 10-second in-process cache.
- `app/channel_router.py` remains the sole request-selection, fallback, failure-threshold, and cooldown authority.
- `app/static/admin_assets/service-reliability.js` owns reliability-page polling and rendering.

## Data Flow

1. Channel and route mutations commit through existing admin APIs.
2. Best-effort reconciliation creates or updates one derived monitor per channel and supported text endpoint, reusing active manual coverage when present.
3. The background monitor loop reconciles at most once per minute, claims one due monitor with a database lease, and immediately executes it through the existing probe engine.
4. `GET /admin/reliability/overview` composes bounded channel, route, runtime, monitor, five-minute traffic, and recent-failure queries, then caches the completed payload for 10 seconds.
5. The admin page polls the overview every 15 seconds while active and visible. Page load and refresh never execute a probe.
6. An explicit operator action may call the retained monitor run endpoint and then invalidate the overview cache.

## Performance Boundary

- No reliability database, Redis, webhook, or network await is imported into customer request-path modules.
- Dashboard reads use six bounded read-only queries and a 10-second in-process cache.
- Route reconciliation runs only in the admin control plane and the existing background monitor loop.
- Probe claims use `FOR UPDATE SKIP LOCKED` only in the background loop, release the database lock before network I/O, and expire automatically if a worker exits before recording results. The scheduled probe's wall-clock timeout is always shorter than its lease.
- Active probe support is limited to `responses` and `chat/completions`; image, video, and embedding routes remain observable through request traffic without automatic active probes.

## Compatibility Boundary

- Provider channel create, update, connection test, model discovery, priority, weight, failure threshold, cooldown, and route CRUD contracts remain stable.
- OpenAI, Anthropic, Claude Code, billing, streaming, and fallback request behavior remain unchanged.
- Protected `/ops/monitoring/*` probes and manual monitor backend APIs remain available.
- Existing manual monitor APIs and persistent probe history are retained.
- A channel with routes or monitor history is disabled instead of hard-deleted; an unreferenced channel still hard-deletes.
- Monitor configurations without probe history are deleted with an otherwise unreferenced channel.

## Reliability States

- Channel states: `unconfigured`, `pending`, `operational`, `degraded`, `cooling`, `failed`, `disabled`.
- Model state is derived from its active routes, route channel state, recent failures, and fallback rate.
- A channel action targets its worst active monitor so an explicit probe addresses the current fault signal.

## Retirement State

- Retired: realtime monitoring navigation/page, ops health navigation/page, embedded manual monitor manager, monitor creation from model discovery, and their dead JavaScript/CSS.
- Retained: manual monitor APIs, protected external probes, monitor/history tables, and existing router behavior.
- Future trigger: add endpoint-specific active probes only when payload, cost, timeout, and success semantics are defined for each non-text endpoint.

## Evidence

- `docs/aegis/work/2026-07-14-service-reliability-center/90-evidence.md`
- `docs/aegis/plans/2026-07-14-service-reliability-center.md`

## Boundary

This baseline is an advisory Aegis workspace snapshot. Runtime code and project contracts remain authoritative.
