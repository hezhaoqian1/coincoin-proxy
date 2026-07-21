# Admin Alert Center - Intent

## TaskIntentDraft

- Requested outcome: Implement, test, ship, merge, and deploy admin alert policy controls and DingTalk delivery history without adding request-path latency.
- Goal: Implement, test, ship, merge, and deploy admin alert policy controls and DingTalk delivery history without adding request-path latency.
- Success evidence:
- none
- Stop condition: Stop when success evidence is satisfied or a blocker/risk requires pause.
- Non-goals:
- Webhook editing or viewing.
- Historical retry and acknowledgement workflows.
- Scope: Runtime non-secret policy overrides, AlertEvent delivery audit, protected admin APIs, existing Service Reliability UI, documentation, tests, review, merge, and deploy verification.
- Change kinds:
- source
- persistence
- admin-ui
- Risk hints:
- No database, Redis, or DingTalk await may enter the customer request coroutine.
- Webhook and raw provider/upstream bodies must never be exposed or persisted.

## BaselineReadSetHint

- docs/aegis/specs/2026-07-21-admin-alert-center-brief.md
- docs/aegis/baseline/service-reliability.md
- docs/architecture/claude-code-upstream-runbook.md

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-07-21-admin-alert-center-brief.md
- docs/aegis/baseline/service-reliability.md
- docs/architecture/claude-code-upstream-runbook.md
- Acknowledged before plan:
- none
- Cited in plan:
- none
- Missing refs:
- docs/aegis/specs/2026-07-21-admin-alert-center-brief.md
- docs/aegis/baseline/service-reliability.md
- docs/architecture/claude-code-upstream-runbook.md
- Advisory decision: needs-baseline-readback

## ImpactStatementDraft

- Compatibility boundary: No public API, routing, fallback order, billing, or RequestLog semantics change.
- Affected layers:
- backend
- database
- admin-ui
- Owners:
- app/fallback_alerts.py
- app/alert_history.py
- app/alert_admin.py
- Invariants:
- Customer request path performs no new awaited I/O.
- Webhook remains environment-only.
- Non-goals:
- Webhook editing or viewing.
- Historical retry and acknowledgement workflows.

These records are Method Pack drafts / hints, not authoritative runtime decisions.

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-07-21-admin-alert-center-brief.md
- docs/aegis/baseline/service-reliability.md
- docs/architecture/claude-code-upstream-runbook.md
- Delivered context refs:
- none
- Acknowledged before plan:
- docs/aegis/specs/2026-07-21-admin-alert-center-brief.md
- docs/aegis/baseline/service-reliability.md
- docs/architecture/claude-code-upstream-runbook.md
- Cited in plan:
- docs/aegis/plans/2026-07-21-admin-alert-center.md
- Missing refs:
- none
- Advisory decision: continue
