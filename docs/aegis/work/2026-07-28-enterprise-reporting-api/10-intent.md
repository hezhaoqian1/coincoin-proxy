# Enterprise Reporting API - Intent

## TaskIntentDraft

- Requested outcome: Implement administrator-managed enterprise reporting credentials for explicitly granted CoinCoin users.
- Goal: Enterprise customers can securely query balances and aggregate usage for only their administrator-selected accounts.
- Success evidence:
- Focused auth/isolation/admin/reporting tests, full backend regression, docs validation, and desktop/mobile admin UI checks pass.
- Stop condition: Done when the additive feature is verified; blocked on unresolved security or baseline failures; scope-exceeded on live customer configuration or deployment.
- Non-goals:
- No production customer setup, deployment, webhook, self-service, raw logs, or write operations.
- Scope: Additive enterprise persistence, dedicated auth/reporting/admin APIs, admin page, tests, and documentation.
- Change kinds:
- feature
- Risk hints:
- Cross-tenant leakage, credential confusion, secret exposure, and billing drift.

## BaselineReadSetHint

- docs/aegis/specs/2026-07-28-enterprise-reporting-api-design.md
- docs/aegis/plans/2026-07-28-enterprise-reporting-api.md
- app/security.py
- app/billing.py
- app/usage_buffer.py
- app/models.py
- app/main.py
- app/static/admin.html

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-07-28-enterprise-reporting-api-design.md
- docs/aegis/plans/2026-07-28-enterprise-reporting-api.md
- app/security.py
- app/billing.py
- app/usage_buffer.py
- app/models.py
- app/main.py
- app/static/admin.html
- Acknowledged before plan:
- none
- Cited in plan:
- none
- Missing refs:
- docs/aegis/specs/2026-07-28-enterprise-reporting-api-design.md
- docs/aegis/plans/2026-07-28-enterprise-reporting-api.md
- app/security.py
- app/billing.py
- app/usage_buffer.py
- app/models.py
- app/main.py
- app/static/admin.html
- Advisory decision: needs-baseline-readback

## ImpactStatementDraft

- Compatibility boundary: Existing API keys, sessions, stations, billing behavior, public routes, and customer data remain unchanged.
- Affected layers:
- persistence
- api
- admin-ui
- documentation
- Owners:
- app/enterprise_reporting.py
- Invariants:
- Only explicit active grants determine visibility; enterprise keys never authorize model calls; plaintext secrets are never persisted.
- Non-goals:
- No production customer setup, deployment, webhook, self-service, raw logs, or write operations.

These records are Method Pack drafts / hints, not authoritative runtime decisions.

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-07-28-enterprise-reporting-api-design.md
- docs/aegis/plans/2026-07-28-enterprise-reporting-api.md
- app/security.py
- app/billing.py
- app/usage_buffer.py
- app/models.py
- app/main.py
- app/static/admin.html
- Delivered context refs:
- none
- Acknowledged before plan:
- docs/aegis/specs/2026-07-28-enterprise-reporting-api-design.md
- docs/aegis/plans/2026-07-28-enterprise-reporting-api.md
- app/security.py
- app/billing.py
- app/usage_buffer.py
- app/models.py
- app/main.py
- app/static/admin.html
- Cited in plan:
- docs/aegis/specs/2026-07-28-enterprise-reporting-api-design.md
- docs/aegis/plans/2026-07-28-enterprise-reporting-api.md
- app/security.py
- app/billing.py
- app/usage_buffer.py
- app/models.py
- app/main.py
- app/static/admin.html
- Missing refs:
- none
- Advisory decision: continue
