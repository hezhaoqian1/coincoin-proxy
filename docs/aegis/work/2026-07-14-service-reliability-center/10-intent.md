# Service Reliability Center - Intent

## TaskIntentDraft

- Requested outcome: Ship a cached unified reliability console with route-derived monitoring and no provider-channel or request-path regression.
- Goal: Ship a cached unified reliability console with route-derived monitoring and no provider-channel or request-path regression.
- Success evidence:
- none
- Stop condition: Stop when success evidence is satisfied or a blocker/risk requires pause.
- Non-goals:
- Automatic health-state enforcement in the request router.
- Dropping monitor tables or deleting persistent rows.
- Scope: Admin reliability read model, route-derived monitor reconciliation, duplicate monitoring UI retirement, focused verification.
- Change kinds:
- feature
- Risk hints:
- Provider-channel compatibility, background probe load, admin query cost, duplicate monitor coverage.

## BaselineReadSetHint

- docs/aegis/plans/2026-07-14-service-reliability-center.md
- app/channel_monitoring.py
- app/admin.py
- app/static/admin.html

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/plans/2026-07-14-service-reliability-center.md
- app/channel_monitoring.py
- app/admin.py
- app/static/admin.html
- Acknowledged before plan:
- none
- Cited in plan:
- none
- Missing refs:
- docs/aegis/plans/2026-07-14-service-reliability-center.md
- app/channel_monitoring.py
- app/admin.py
- app/static/admin.html
- Advisory decision: needs-baseline-readback

## ImpactStatementDraft

- Compatibility boundary: Provider channel CRUD, connection test, upstream model discovery, route CRUD, fallback, priority, weight, cooldown, and protected ops probes remain stable.
- Affected layers:
- admin control plane
- Owners:
- app/reliability.py and app/channel_monitoring.py
- Invariants:
- No request-path database, Redis, webhook, or probe I/O is added.
- Non-goals:
- Automatic health-state enforcement in the request router.
- Dropping monitor tables or deleting persistent rows.

These records are Method Pack drafts / hints, not authoritative runtime decisions.
