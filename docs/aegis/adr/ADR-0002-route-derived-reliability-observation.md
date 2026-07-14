# ADR-0002 - Use route-derived cached reliability observation

Status: `recorded-from-work`
Date: `2026-07-14`

## Source Evidence

- Implemented and verified by the service reliability center work record and four-task plan.
## Context

The admin had overlapping monitoring pages, page-load probes, and manual monitor setup that duplicated provider route configuration. Reliability observation needed to expose model and channel failures without adding network or database work to customer requests.

## Decision

Model routes are the source of truth for supported text monitor targets. app/reliability.py owns a bounded cached admin read model, while app/channel_router.py remains the sole request-selection and fallback authority. Dashboard polling is read-only; only an explicit operator action may execute a probe.

Scheduled probes use a database-backed `claimed_until` lease and claim one monitor immediately before execution. A wall-clock timeout shorter than the lease bounds the entire probe run. This keeps multiple application workers or replicas from probing the same channel concurrently without holding database locks during upstream network calls.

## Alternatives Considered

- Keep manual monitor setup as a separate admin workflow; rejected because it duplicates route configuration and drifts after channel changes.
- Execute live probes whenever the dashboard loads or refreshes; rejected because it adds cost, latency, and accidental load to observation.
- Let reliability health directly enable or disable request routes; deferred because phase one must preserve the existing router and fallback contract.
## Consequences

- New channel routes appear in the reliability console automatically, and overview reads are cached for ten seconds.
- Manual monitor APIs and persistent history remain compatibility boundaries while duplicate admin UI is retired.
- Channels with route or probe history are disabled instead of deleted; monitor configurations without history are removed with an otherwise unreferenced channel.
- Unsupported image, video, and embedding endpoints remain traffic-observed and are not auto-probed until endpoint-specific probes are deliberately implemented.
## Compatibility Boundary

Provider channel CRUD, connection testing, model discovery, route priority and weight, fallback, cooldown, protected ops probes, and manual monitor APIs remain stable. No reliability I/O is added to customer request paths.

## Retirement Impact

The realtime monitoring page, ops health page, embedded manual monitor manager, and their dead JavaScript/CSS are retired. Probe history is retained; empty monitor configurations do not prevent an otherwise unreferenced channel from hard-deleting.

## Baseline Sync

- Needed: needed
- Target: docs/aegis/baseline/service-reliability.md
- Action: create snapshot
- Reason: The project needs a current ownership, dependency, compatibility, probe-support, and retirement snapshot linked to this ADR.

## Evidence References

- docs/aegis/work/2026-07-14-service-reliability-center/90-evidence.md
- docs/aegis/plans/2026-07-14-service-reliability-center.md
## Boundary

This ADR is an advisory Aegis Method Pack record. It does not grant completion authority or replace project-authoritative architecture sources.

## Amendment - 2026-07-15 - Adopt one representative real generation probe per provider channel and separate channel probe health from public-model routing and real-traffic health.

- Status: amended

### Source Evidence

- Implemented by commits 3f2a8ee through f562b34 on codex/channel-probe-model.
### Change Summary

Adopt one representative real generation probe per provider channel and separate channel probe health from public-model routing and real-traffic health.

### Amended Decision

- Each active provider channel has at most one active representative monitor. Automatic selection is deterministic: lowest effective route priority, then highest effective route weight, then stable route ID.
- An administrator may select an exact model and endpoint pair from the channel's active supported routes or reset the channel to automatic selection. The provider-channel modal owns this workflow.
- Each probe sends one non-streaming real generation request with `Reply with OK.` to the selected route endpoint. It performs exactly one `POST`, performs no `/models` preflight, and records success only for a 2xx response with structurally valid model output.
- Probe state contributes only to provider-channel health. Public-model health never inherits a channel probe result; it is derived from active route coverage, endpoint-isolated real request traffic, request failures, correctly source-attributed fallback, a 30-second average-latency threshold, and request-router cooldown.
- Monitoring is observation-only. It never changes channel or route priority, weight, route status, cooldown, fallback behavior, or request routing. `app/channel_router.py` remains the sole request-selection and fallback authority.
- The reliability console is channel-first. Channel summary, incidents, representative target, and explicit probe action precede public-model routing and real-traffic details.
- `fallback_from_channel_id` is widened to `VARCHAR(512)` so a request can retain multiple fallback source channel IDs for attribution. The application performs no data `UPDATE` or `DELETE` and preserves existing values; this record makes no claim about MySQL's internal DDL rebuild behavior.

### Compatibility Boundary

Provider channel and route CRUD, priority, weight, route status, router cooldown, fallback, request routing, streaming, billing, manual monitor APIs, extra_models persistence/API shape, and retained probe history remain compatible. `fallback_from_channel_id` is widened to `VARCHAR(512)` without application-level data `UPDATE` or `DELETE`, and existing values are preserved; MySQL may internally rebuild storage while applying the DDL.

### Retirement Impact

Legacy multi-model execution and redundant automatic monitors are retired from execution. extra_models and history remain persisted for compatibility, while reconciliation clears executable extras and disables redundant automatic monitors without deleting history.

### Baseline Sync

- Needed: needed
- Target: docs/aegis/baseline/service-reliability.md
- Action: update baseline
- Reason: The current snapshot must record channel-level representative selection, channel-only probe health, public-model real-traffic health, UI ownership, compatibility retention, and migration widening.

### Evidence References

- docs/aegis/work/2026-07-15-channel-representative-probe/90-evidence.md
- docs/aegis/specs/2026-07-15-channel-representative-probe-design.md
- docs/aegis/plans/2026-07-15-channel-representative-probe.md
### Boundary

This amendment is an advisory Aegis Method Pack record. It does not grant completion authority or replace project-authoritative architecture sources.
