# Service Reliability Baseline

Status: `current-state-snapshot`
Date: `2026-07-22`
Decision: `docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md`
Alert decision: `docs/aegis/adr/ADR-0003-alert-delivery-audit-boundary.md`
Webhook decision: `docs/aegis/adr/ADR-0004-admin-managed-alert-webhook.md`

## Ownership Map

- Active `ModelChannelRoute` rows are the source of truth for supported representative probe targets and configured delivery paths.
- `ProviderChannelMonitor` remains the persistence and API compatibility owner for representative selection, lease state, latest result, retained history, and daily rollups. At most one active representative monitor per provider channel is the canonical invariant.
- `app/channel_monitoring.py` owns deterministic representative selection, reconciliation, background claims, single-request probe execution, retained history, and daily rollups.
- `app/admin.py` owns validation of exact administrator model and endpoint overrides and reset to automatic selection.
- `app/reliability.py` owns the bounded admin read model and its 10-second in-process cache.
- `app/channel_router.py` remains the sole request-selection, fallback, failure-threshold, and cooldown authority.
- The provider-channel modal owns representative selection. `app/static/admin_assets/service-reliability.js` owns channel-first reliability polling, rendering, route detail, and explicit probe actions.
- `app/fallback_alerts.py` owns user-path burst detection, bounded task scheduling, and DingTalk delivery. `RequestLog` owns every upstream-failure attempt; `AlertEvent` owns only actual outbound notification attempts. `app/alert_admin.py` owns the protected alert policy, complete webhook configuration, configuration-test, and bounded history API. The `fallback_alert_webhook_url` `SystemSetting` key is the primary webhook owner; the Railway environment value is used only when that database key is absent.

## Representative Selection

1. Reconciliation considers only active channels and active routes with supported text generation endpoints.
2. Automatic selection sorts routes by lowest effective priority, highest effective weight, and stable route ID. Route overrides are effective when present; otherwise channel priority and weight apply.
3. The selected target is the route's exact `upstream_model` and normalized supported endpoint. Anthropic-compatible routes without an endpoint use `chat/completions` probe semantics.
4. An administrator may select an exact model and endpoint pair from the channel's active supported route choices. Resetting to automatic re-enables the deterministic target.
5. A valid manual override is retained until reset or until the exact route target is no longer active and supported. Invalid manual targets remain visible as invalid and are not silently replaced.
6. Channels without a supported active text route are unconfigured for active probing.

## Probe Contract

1. The background loop reconciles at most once per minute, claims one due monitor with a database lease, and executes one representative probe.
2. A probe sends `Reply with OK.` as one minimal non-streaming generation request to the selected endpoint.
3. A probe performs exactly one upstream `POST`, performs no `/models` preflight, and does not require the literal output `OK.`.
4. A successful probe requires a 2xx response with structurally valid model output and no error payload. Timeout, request, response-shape, and non-2xx failures are recorded against the channel monitor.
5. Page load, polling, reconciliation, and selection updates do not execute probes. Only the background schedule or explicit operator probe action performs upstream generation I/O.

## Reliability Semantics

- Representative probe status affects provider-channel health only. It never marks every public model on that channel failed.
- Channel health combines configured/route coverage, representative probe state, real request failures, correctly source-attributed fallback-out traffic, average latency, and router cooldown.
- Public-model health uses active route coverage and route health derived from endpoint-isolated real traffic, correctly source-attributed fallback, the 30-second average-latency degradation threshold, and router cooldown.
- Fallback source attribution uses each complete channel ID retained in `fallback_from_channel_id`; the destination channel is not blamed for the source channel's fallback-out event. The persisted field remains the legacy 32-character contract, so multi-hop attribution is best-effort and guarantees the first complete source ID only.
- Endpoint normalization keeps `responses`, `chat/completions`, image-generation aliases, and other route traffic isolated before route health is computed.
- Monitoring never mutates priority, weight, route status, channel status, cooldown, fallback, or request routing.

## Admin Read Flow

1. `GET /admin/reliability/overview` composes bounded channel, route, runtime, monitor, five-minute traffic, and recent-failure queries, then caches the completed payload for 10 seconds.
2. The reliability page polls every 15 seconds only while active and visible. Channels and channel incidents render before public-model routing and real-traffic health.
3. The provider-channel modal displays automatic selection, current exact manual selection, invalid manual state, active supported choices, and reset to automatic.
4. An explicit operator action calls the retained monitor run endpoint and invalidates the overview cache after completion.
5. While the same page is active, `GET /admin/alerts/config` and `GET /admin/alerts/events` load the complete DingTalk destination, runtime policy, latest success/failure times, and at most 100 indexed delivery records. Config responses are protected and non-cacheable. Saving writes the complete validated policy and plaintext webhook to `SystemSetting`; an empty webhook is an explicit disable override, and the configuration-test action sends one labelled message and records the attempt.

## Performance Boundary

- No reliability database, Redis, webhook, or network await executes in the customer request coroutine.
- Dashboard reads use six bounded read-only queries and a 10-second in-process cache.
- Route reconciliation runs only in the admin control plane and the existing background monitor loop.
- Probe claims use `FOR UPDATE SKIP LOCKED` only in the background loop, release the database lock before network I/O, and expire automatically if a worker exits before recording results. The scheduled probe's wall-clock timeout is always shorter than its lease.
- Active probe support is limited to one representative `responses` or `chat/completions` target per provider channel; image, video, and embedding routes remain traffic-observed without automatic active probes.
- Successful customer requests execute no alert work. Failed requests retain only the existing synchronous classification, bounded in-memory queue check, and `asyncio.create_task` scheduling. Redis, `AlertEvent` database writes, and DingTalk network awaits occur only inside tracked background tasks after scheduling. A threshold alert is delivered by its existing counter task rather than a nested task, and each best-effort audit write is capped at 250 ms.
- Dedup-suppressed failures create no `AlertEvent` write; they remain visible through `RequestLog`. Admin alert reads run only while the reliability page is active, use indexed event fields, and cap each history query at 100 rows.

## Compatibility Boundary

- Provider channel create, update, connection test, model discovery, priority, weight, failure threshold, cooldown, and route CRUD contracts remain stable.
- OpenAI, Anthropic, Claude Code, billing, streaming, and fallback request behavior remain unchanged.
- Protected `/ops/monitoring/*` probes and manual monitor backend APIs remain available.
- Existing manual monitor APIs, the `extra_models` persistence/API field, and persistent probe history are retained. `extra_models` is not executed by representative probes and is normalized empty when a monitor is reconciled or selected.
- Redundant automatic monitors are disabled rather than deleted, preserving their history.
- `fallback_from_channel_id` remains `VARCHAR(32)` in the SQLAlchemy model, create-table DDL, and buffered writer. No hot-table width migration runs during application startup; a wider multi-hop audit contract requires a separately operated migration outside the service health-check path.
- A channel with routes or monitor history is disabled instead of hard-deleted; an unreferenced channel still hard-deletes.
- Monitor configurations without probe history are deleted with an otherwise unreferenced channel.
- The webhook value is stored in plaintext in `SystemSetting` and returned only through the protected, `Cache-Control: no-store` alert config API. A present database key is authoritative even when empty; the Railway value remains a bootstrap fallback only while the key is absent. Alert history never contains webhook/API keys, raw upstream/Cloudflare content, or raw DingTalk response bodies.

## Reliability States

- Channel states: `unconfigured`, `pending`, `operational`, `degraded`, `cooling`, `failed`, `disabled`.
- Public-model state is derived from active route coverage, endpoint-isolated real traffic, failures, fallback attribution, average latency, and router cooldown. It does not consume representative probe state.
- The channel action targets the channel's single active representative monitor.

## Retirement State

- Retired: multi-model and per-endpoint automatic probe execution, `/models` probe preflight, public-model health inherited from channel probe state, realtime monitoring navigation/page, ops health navigation/page, embedded manual monitor manager, monitor creation from model discovery, and their dead JavaScript/CSS.
- Retired: Railway-only webhook ownership.
- Retained: `extra_models` persistence/API compatibility, manual monitor APIs, protected external probes, monitor/history tables, disabled redundant monitor rows, existing router behavior, and the Railway webhook variable as an absent-key compatibility fallback.
- Future trigger: add endpoint-specific active probes only when payload, cost, timeout, and success semantics are defined for each non-text endpoint.

## Evidence

- `docs/aegis/work/2026-07-14-service-reliability-center/90-evidence.md`
- `docs/aegis/plans/2026-07-14-service-reliability-center.md`
- `docs/aegis/work/2026-07-15-channel-representative-probe/90-evidence.md`
- `docs/aegis/specs/2026-07-15-channel-representative-probe-design.md`
- `docs/aegis/plans/2026-07-15-channel-representative-probe.md`
- Implementation commits `3f2a8ee`, `41d4d7b`, `72e1a4b`, `bb8bd40`, `782ec2f`, `3b5b3a0`, `41b1ce7`, `b93e7fe`, `db2a4fb`, `a9e4034`, `9a925de`, `d86e747`, and `f562b34`.

## Boundary

This baseline is an advisory Aegis workspace snapshot. Runtime code and project contracts remain authoritative.
