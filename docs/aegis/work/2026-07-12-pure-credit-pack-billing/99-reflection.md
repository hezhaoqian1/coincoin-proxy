# Pure credit pack billing implementation - Reflection

## Completion Candidate Reflection

- Goal: New buyers can purchase only three permanent USD credit packs, while existing paid monthly and historical traffic-pack value remains usable without value loss.
- DeeperCause: no. The old VPN-style product model was removed from the public catalog, order validation, payment confirmation, Recharge UI, and unreachable purchase helpers; remaining legacy owners are tied to real persisted entitlements.
- Evidence: focused wallet/payment/admin/migration/video suites, full Python regression, frontend production build, Go tests, lingering-reference scan, independent Task 5 reviews, and dry-run migration safety tests.
- Risk/Unknown: live migration and real MySQL concurrency were not exercised; three unrelated video tests remain on the pre-existing RequestLog schema mismatch; browser assistive-technology behavior was not manually tested.
- Decision: completion candidate verified by final independent review, workspace proof bundle/check, ADR-0001, and the synchronized architecture baseline.

## Repair Track

- Repaired object: new purchase and usage billing ownership.
- Action: permanent batches became the canonical new-money owner; payment promises are frozen; usage and exact refunds include wallet allocations; payloads and UI expose wallet plus total availability.
- Impact: new purchases no longer inherit duration, proration, monthly gates, or add-on semantics.
- Verification: focused and full regression evidence recorded in `90-evidence.md`.

## Retirement Track

- Retired object: public monthly/add-on catalog, upgrade/reset/renew/proration execution, monthly-gated add-on purchase, implicit serializer wallet bridge, and stale customer sales styles/copy.
- Retained boundary: historical product metadata, active monthly normalization/debit, valid traffic-pack debit, scalar balance compatibility, and admin correction controls.
- Future trigger: remove traffic-pack/scalar owners only after a verified live migration; remove subscription compatibility only after the final live `paid_until` expires and production confirms no active rows.

Method Pack output does not grant completion authority.
