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
