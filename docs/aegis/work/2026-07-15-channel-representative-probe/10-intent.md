# Channel Representative Probe - Intent

## TaskIntentDraft

- Requested outcome: Monitor each provider channel with one highest-priority representative model, allow administrator override, test, ship, and verify production.
- Goal: Monitor each provider channel with one highest-priority representative model, allow administrator override, test, ship, and verify production.
- Success evidence:
- Focused and compatibility tests pass; channel-first browser QA passes; production representative probe succeeds without routing changes.
- Stop condition: Done only after merged deployment and production verification; otherwise report blocked, needs-verification, or scope-exceeded.
- Non-goals:
- No automatic route mutation from probe results.
- No active image/video/embedding probes.
- Scope: Channel monitor selection, probe execution, admin selection UI/API, reliability semantics, compatibility retirement, tests, docs, and deployment QA.
- Change kinds:
- architecture
- Risk hints:
- Wrong ownership could alter routing semantics or multiply probe traffic.

## BaselineReadSetHint

- docs/aegis/specs/2026-07-15-channel-representative-probe-design.md
- docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md
- docs/aegis/baseline/service-reliability.md

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-07-15-channel-representative-probe-design.md
- docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md
- docs/aegis/baseline/service-reliability.md
- Acknowledged before plan:
- docs/aegis/specs/2026-07-15-channel-representative-probe-design.md
- docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md
- docs/aegis/baseline/service-reliability.md
- Cited in plan:
- docs/aegis/specs/2026-07-15-channel-representative-probe-design.md
- docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md
- docs/aegis/baseline/service-reliability.md
- Missing refs:
- none
- Advisory decision: continue

## ImpactStatementDraft

- Compatibility boundary: Provider channel and route CRUD, priority, weight, route status, cooldown, fallback, request routing, streaming, billing, manual monitor APIs, `extra_models` persistence/API compatibility, and retained history remain stable. `fallback_from_channel_id` is widened to 512 without application-level data `UPDATE` or `DELETE`, and existing values are preserved; MySQL may internally rebuild storage while applying the DDL.
- Affected layers:
- monitor control plane
- admin UI
- reliability read model
- request-log fallback attribution persistence
- Owners:
- app/channel_monitoring.py
- app/admin.py
- app/reliability.py
- app/models.py
- app/main.py
- app/usage_buffer.py
- Invariants:
- app/channel_router.py remains the sole routing and fallback authority.
- Monitoring does not mutate priority, weight, route status, cooldown, fallback, or request routing.
- Non-goals:
- No automatic route mutation from probe results.
- No active image/video/embedding probes.

These records are Method Pack drafts / hints, not authoritative runtime decisions.
