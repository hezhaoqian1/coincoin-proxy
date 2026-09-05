# ADR-0001 - Use permanent credit batches for all new paid value

Status: `recorded-from-work`
Date: `2026-07-12`

## Source Evidence

- Implemented and verified by the pure credit pack billing work record and six-task plan.
## Context

The previous VPN-style catalog sold monthly and traffic-pack products even though customers primarily wanted spendable USD value. A scalar balance lacked payment provenance and exact asynchronous refund allocations, while existing monthly and traffic-pack records remain paid customer entitlements.

## Decision

All new paid value is sold as one of three permanent USD credit products and granted into immutable-provenance credit batches. Billing orchestrates active legacy monthly entitlement, valid historical traffic packs, permanent credit batches, then unmigrated scalar balance. New orders freeze catalog version, purchase action, and promised cents.

## Alternatives Considered

- Keep monthly products and repair proration, renewal, reset, and traffic-pack rules; rejected because duration and gate semantics do not match the product being sold.
- Write new payments directly into User.balance; rejected because the scalar cannot prove order idempotency, FIFO consumption, or exact allocation refunds.
## Consequences

- New credits do not expire and can be stacked. CreditBalance and CreditAllocation become durable billing records, and payment/catalog/API/UI contracts expose the wallet explicitly.
- Legacy subscription, traffic-pack, and positive scalar sources remain bounded compatibility owners until paid periods expire or a separately authorized live migration retires them.
## Compatibility Boundary

Existing paid monthly periods remain usable through paid_until. Valid historical traffic packs and unmigrated scalar balances remain spendable. Legacy pending orders are never guessed or auto-confirmed. No live cleanup or migration is part of this decision record.

## Retirement Impact

Public monthly/add-on sales, proration, renew/reset/upgrade execution, and the monthly gate on historical pack usage are retired. Traffic-pack/scalar debit paths retire after verified migration; subscription debit retires after the final live paid_until.

## Baseline Sync

- Needed: needed
- Target: docs/aegis/baseline/pure-credit-pack-billing.md
- Action: create snapshot
- Reason: The project needs a current owner, debit-order, compatibility, migration, and retirement snapshot linked to this ADR.

## Evidence References

- docs/aegis/work/2026-07-12-pure-credit-pack-billing/proof-bundle.md
- docs/aegis/plans/2026-07-12-pure-credit-pack-billing.md
- README.zh-CN.md
## Boundary

This ADR is an advisory Aegis Method Pack record. It does not grant completion authority or replace project-authoritative architecture sources.
