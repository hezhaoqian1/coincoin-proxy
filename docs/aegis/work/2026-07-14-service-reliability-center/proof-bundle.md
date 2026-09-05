# Proof Bundle - 2026-07-14-service-reliability-center

## Method Pack Boundary

This proof bundle is an advisory Aegis Method Pack record. It does not determine evidence sufficiency, produce authoritative `GateDecision`, or grant `completion authority`.

## Task Intent

- Requested outcome: Ship a cached unified reliability console with route-derived monitoring and no provider-channel or request-path regression.
- Scope: Admin reliability read model, route-derived monitor reconciliation, duplicate monitoring UI retirement, focused verification.

## Impact

- Compatibility boundary: Provider channel CRUD, connection test, upstream model discovery, route CRUD, fallback, priority, weight, cooldown, and protected ops probes remain stable.
- Non-goals:
- Automatic health-state enforcement in the request router.
- Dropping monitor tables or deleting persistent rows.

## Evidence Bundle Refs

- docs/aegis/work/2026-07-14-service-reliability-center/evidence-bundle-draft-task1-focused-tests.json
- docs/aegis/work/2026-07-14-service-reliability-center/evidence-bundle-draft-task2-reconcile-tests.json
- docs/aegis/work/2026-07-14-service-reliability-center/evidence-bundle-draft-task3-browser-ui.json
- docs/aegis/work/2026-07-14-service-reliability-center/evidence-bundle-draft-task4-compat-review.json

## Drift Check

- Scope status: All slices stayed inside the admin reliability console, background control-plane reconciliation, duplicate UI retirement, and verification fence.
- Compatibility status: Request routing is unchanged; reconciliation failures roll back before later admin work, and monitored channels disable instead of violating retained-history foreign keys.
- Retirement status: Duplicate admin UI and dead styles are removed; manual monitor APIs and persistent monitor/history rows remain intact.
- Advisory decision: continue
