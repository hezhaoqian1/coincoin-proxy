# Alert Webhook Management - Intent

## TaskIntentDraft

- Requested outcome: Allow administrators to fully view, modify, clear, and test the DingTalk Webhook from Service Reliability, then initialize the approved production value.
- Goal: Allow administrators to fully view, modify, clear, and test the DingTalk Webhook from Service Reliability, then initialize the approved production value.
- Success evidence:
- Backend/UI regression tests pass, PR merged, Railway healthy, live admin GET returns the exact saved value, and a configuration test records sent.
- Stop condition: Done after verified production initialization; blocked on repeated test/deploy failure; needs verification when live mutation cannot be confirmed; scope exceeded if routing or alert counters must change.
- Non-goals:
- Encrypting the database value.
- Adding a new page or endpoint.
- Scope: Existing alert admin API, SystemSetting runtime precedence, fallback senders, Service Reliability form, architecture records, deployment, and one authorized live initialization.
- Change kinds:
- architecture
- Risk hints:
- Plaintext credential persistence and full admin exposure are explicit accepted risks; production URL must stay out of Git and logs.

## BaselineReadSetHint

- docs/aegis/specs/2026-07-22-alert-webhook-management-brief.md
- docs/aegis/adr/ADR-0003-alert-delivery-audit-boundary.md
- docs/aegis/baseline/service-reliability.md

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-07-22-alert-webhook-management-brief.md
- docs/aegis/adr/ADR-0003-alert-delivery-audit-boundary.md
- docs/aegis/baseline/service-reliability.md
- Acknowledged before plan:
- none
- Cited in plan:
- none
- Missing refs:
- docs/aegis/specs/2026-07-22-alert-webhook-management-brief.md
- docs/aegis/adr/ADR-0003-alert-delivery-audit-boundary.md
- docs/aegis/baseline/service-reliability.md
- Advisory decision: needs-baseline-readback

## ImpactStatementDraft

- Compatibility boundary: Public APIs, routing, billing, RequestLog, alert counters, and AlertEvent semantics remain unchanged.
- Affected layers:
- FastAPI admin control plane
- Runtime alert settings
- Static admin UI
- Owners:
- app/alert_admin.py
- app/fallback_alerts.py
- app/system_settings.py
- Invariants:
- Database row presence wins, including empty; row absence falls back to environment.
- No database or network await enters a customer request path.
- Non-goals:
- Encrypting the database value.
- Adding a new page or endpoint.

These records are Method Pack drafts / hints, not authoritative runtime decisions.
