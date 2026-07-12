# Pure Credit Pack Billing Baseline

Status: current architecture snapshot
Decision: [ADR-0001](../adr/ADR-0001-permanent-credit-wallet.md)
Updated: 2026-07-12

## Product Contract

- New customers can buy only these permanent 美金额度 products:
  - ¥59.90 -> $100
  - ¥199 -> $400
  - ¥399 -> $1,000
- New purchased value has no duration, does not expire, and stacks with existing permanent credit.
- Public purchase creation and confirmation accept only frozen registered credit-catalog products.

## Canonical Owners

- `app/credit_wallet.py` owns permanent paid credit batches, FIFO allocation, and exact allocation refunds.
- `app/payment.py` and `app/payment_common.py` own order creation, frozen promises, confirmation, and idempotent grant orchestration.
- `app/billing.py` owns cross-source availability and debit orchestration; it does not own permanent batch persistence.
- `scripts/migrate_legacy_credits.py` owns dry-run planning and separately authorized legacy-source migration.

## Availability And Debit Order

1. Active legacy monthly entitlement for the current normalized period.
2. Active, positive, unexpired historical traffic packs.
3. Active permanent credit batches in FIFO order.
4. Unmigrated scalar `User.balance`, including any negative debt offset.

## Compatibility Boundary

- Existing monthly subscriptions remain usable until their persisted `paid_until`; their 30-day period rollover semantics remain active.
- Existing valid traffic packs remain usable without an active monthly subscription.
- Positive scalar balance remains spendable until migrated; negative scalar balance remains debt and is not converted into credit.
- Historical product IDs and metadata remain available for old records and admin correction controls.
- Existing pending legacy orders are quarantined from automatic confirmation.
- `credit_wallet` is canonical in payloads; `credit_balance` is a temporary read compatibility alias.

## Retirement Boundary

- Retired now: public monthly/add-on catalog, proration, upgrade, renew, reset, monthly-gated add-on purchase, new-payment scalar writes, and hidden serializer wallet state.
- Retire after verified live migration: traffic-pack and positive scalar-balance debit compatibility.
- Retire after production confirms the final paid period has ended: subscription normalization and debit compatibility.
- Persistent tables, columns, rows, and historical billing records are not deleted by this implementation.

## Migration And Operations

- Migration defaults to dry-run and reports integer-cent conservation and unsupported states.
- Apply requires explicit scan and plan limits, locked fingerprint recheck, all-or-nothing source retirement/grant, and post-commit reconciliation.
- Normal application startup and deployment do not run the live migration.
- Production apply, deployment, and irreversible cleanup require separate operator authorization and evidence.

## Verification Baseline

- Full Python regression may retain only the three recorded video failures caused by `RequestLog.effective_cache_creation_input_per_million` constructor drift.
- Frontend production build and `usage-quota-service` Go tests must pass.
- Real MySQL/InnoDB concurrency, savepoint, and migration sizing remain an integration evidence gap until exercised against an approved environment.
