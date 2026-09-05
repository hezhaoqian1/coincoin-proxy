# Pure credit pack billing implementation - Intent

## TaskIntentDraft

- Requested outcome: Replace new VPN-style monthly purchases with three permanent USD credit packs while honoring existing monthly entitlements and safely retiring traffic-pack sales.
- Goal: Replace new VPN-style monthly purchases with three permanent USD credit packs while honoring existing monthly entitlements and safely retiring traffic-pack sales.
- Success evidence:
- New products create permanent credit batches; legacy monthly entitlements remain spendable; legacy traffic packs can be dry-run migrated without value drift; billing/payment/admin/UI tests pass; full suite has no new failures beyond the three recorded video baseline failures.
- Stop condition: Done only with verified tests and zero-value-drift migration evidence; otherwise stop as blocked, needs-verification, or scope-exceeded.
- Non-goals:
- No live database migration, deployment, price profitability claim, or confirmation of pending legacy orders.
- Scope: Billing models, product catalog, payment confirmation, balance debit/refund, migration tooling, admin/user billing payloads, recharge UI, and targeted tests.
- Change kinds:
- migration
- Risk hints:
- High-risk billing source-of-truth and persistent-state migration; no live apply or destructive database action in this task.

## BaselineReadSetHint

- /Users/windupbird/.gstack/projects/hezhaoqian1-coincoin-proxy/windupbird-master-design-20260712-055650.md
- app/billing.py
- app/payment_common.py
- app/models.py
- app/main.py
- tests/test_subscription_billing.py

## BaselineUsageDraft

- Required baseline refs:
- /Users/windupbird/.gstack/projects/hezhaoqian1-coincoin-proxy/windupbird-master-design-20260712-055650.md
- app/billing.py
- app/payment_common.py
- app/models.py
- app/main.py
- tests/test_subscription_billing.py
- Acknowledged before plan:
- none
- Cited in plan:
- none
- Missing refs:
- /Users/windupbird/.gstack/projects/hezhaoqian1-coincoin-proxy/windupbird-master-design-20260712-055650.md
- app/billing.py
- app/payment_common.py
- app/models.py
- app/main.py
- tests/test_subscription_billing.py
- Advisory decision: needs-baseline-readback

## ImpactStatementDraft

- Compatibility boundary: Existing active monthly periods continue until paid_until; old product IDs are not offered for new sales.
- Affected layers:
- billing persistence
- payment confirmation
- usage debit/refund
- admin and customer UI
- Owners:
- app/billing.py and new credit wallet owner
- Invariants:
- Every confirmed payment grants exactly the frozen promised USD cents once.
- Existing paid monthly entitlements and valid traffic-pack balances are never silently lost or duplicated.
- Non-goals:
- No live database migration, deployment, price profitability claim, or confirmation of pending legacy orders.

These records are Method Pack drafts / hints, not authoritative runtime decisions.
