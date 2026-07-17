# CoinCoin Admin Performance Optimization - Intent

## TaskIntentDraft

- Requested outcome: Remove intermittent admin-console stalls without changing billing, routing, payment, or public API behavior.
- Goal: Remove intermittent admin-console stalls without changing billing, routing, payment, or public API behavior.
- Success evidence:
- Bounded user-list SQL calls; one-query dashboard leaderboards; single-flight analytics cache; warm provider page avoids all-history scans; admin timing headers; passing regressions.
- Stop condition: Done when all planned tests and checks pass; otherwise stop as blocked, needs-verification, or scope-exceeded before schema or production changes.
- Non-goals:
- No database schema, persistent rollup, deployment, or production mutation.
- Scope: Admin read-path performance in billing lists, dashboard leaderboards, operating analytics, provider-channel statistics, UI pagination, and request timing.
- Change kinds:
- performance-repair
- Risk hints:
- Preserve billing serialization and admin response compatibility; no schema or live-state mutation.

## BaselineReadSetHint

- docs/aegis/specs/2026-07-17-admin-performance-optimization-brief.md
- docs/aegis/plans/2026-05-13-operating-dashboard-v2.md
- app/admin.py
- app/billing.py
- tests/test_admin_usage_fields.py

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-07-17-admin-performance-optimization-brief.md
- docs/aegis/plans/2026-05-13-operating-dashboard-v2.md
- app/admin.py
- app/billing.py
- tests/test_admin_usage_fields.py
- Acknowledged before plan:
- none
- Cited in plan:
- none
- Missing refs:
- docs/aegis/specs/2026-07-17-admin-performance-optimization-brief.md
- docs/aegis/plans/2026-05-13-operating-dashboard-v2.md
- app/admin.py
- app/billing.py
- tests/test_admin_usage_fields.py
- Advisory decision: needs-baseline-readback

## ImpactStatementDraft

- Compatibility boundary: Existing admin fields and /admin/usage/leaderboard remain compatible; only statistics receive bounded cache freshness.
- Affected layers:
- admin-ui
- admin-api
- billing-read-model
- database-query-load
- Owners:
- app/billing.py: billing-state reads
- app/admin.py: admin aggregation APIs
- app/main.py: request timing
- Invariants:
- Billing debit, payment, routing, and public API behavior do not change.
- Non-goals:
- No database schema, persistent rollup, deployment, or production mutation.

These records are Method Pack drafts / hints, not authoritative runtime decisions.
