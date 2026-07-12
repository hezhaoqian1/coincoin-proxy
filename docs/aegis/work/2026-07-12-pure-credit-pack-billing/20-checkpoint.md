# Pure credit pack billing implementation - Checkpoint

- Task ID: 2026-07-12-pure-credit-pack-billing
- Current todo: Write implementation plan and baseline map.
- Active slice: Planning and source-of-truth boundary.
- Blocked on: Three unrelated video tests fail on baseline due to missing RequestLog cache-pricing field.
- Next step: Save and self-review the implementation plan, then execute inline.

## Checkpoint Update

- Current todo: Task 1: add permanent-credit persistence and wallet owner.
- Active slice: CreditBalance/CreditAllocation persistence and app/credit_wallet.py.
- Completed todos:
- Approved design captured and implementation plan saved.
- Isolated codex/credit-packs worktree created.
- Evidence refs:
- docs/aegis/plans/2026-07-12-pure-credit-pack-billing.md
- Baseline: 426 passed, 3 unrelated video failures, 4 skipped.
- Blocked on: No task-local blocker.
- Next step: Dispatch Task 1 implementer, then run spec and code-quality reviews.

## DriftCheckDraft

- Scope status: Inside approved billing/payment/migration/UI scope.
- Compatibility status: Legacy monthly and unmigrated balances explicitly preserved.
- Retirement status: Old sales retire immediately; data paths retire only after migration/final paid_until.
- New risk signals:
- none
- Advisory decision: continue

## Task 1 Completion Update

- Current todo: Task 2: replace the public catalog and freeze new order semantics.
- Active slice: Credit product catalog, frozen order fields, and idempotent payment confirmation.
- Completed todos:
- Task 1 permanent-credit persistence and wallet owner implemented.
- Task 1 specification review passed after concurrency and schema-parity fixes.
- Task 1 code-quality review approved after lock-order, unique-index failure, UTC, and financial-constraint fixes.
- Evidence refs:
- `tests/test_credit_wallet.py`: 17 passed, 7 subtests passed.
- `tests/test_credit_wallet.py tests/test_subscription_billing.py`: 26 passed, 7 subtests passed.
- `git diff --check`: passed.
- Blocked on: No Task 2 blocker. Real MySQL multi-connection integration remains unavailable because the supplied local test credentials are rejected.
- Next step: Dispatch Task 2 implementer with the frozen `credit-v1` catalog and legacy-order quarantine boundary.

## Task 1 Drift Check

- Scope status: Inside the approved additive persistence/wallet slice.
- Compatibility status: Existing subscription, traffic-pack, scalar-balance, payment, and UI behavior is unchanged.
- Retirement status: No old owner retired in Task 1; the new wallet exists but is not yet wired to public sales or usage.
- New risk signals:
- Real InnoDB savepoint, row-lock, and CHECK enforcement lack live integration evidence.
- Advisory decision: continue; retain the integration limitation in final verification evidence.

## Task 2 Completion Update

- Current todo: Task 3: route availability, debit, and video refunds through the wallet.
- Active slice: Billing source ordering, permanent-credit allocations, and async video refund persistence.
- Completed todos:
- Public catalog now contains exactly three permanent USD credit products.
- New orders freeze registered catalog version, credit purchase action, and promised USD cents.
- Payment confirmation grants one permanent wallet batch from frozen terms and rejects unfrozen legacy pending orders.
- Finance, referral, and station effects share the common confirmation transaction; confirmed replays can backfill a missing station entry exactly once.
- Task 2 specification review passed after product-version allowlist and common station-owner fixes.
- Task 2 code-quality review approved after catalog source consolidation, station replay commit cleanup, admin audit fields, and historical-version rollover support.
- Evidence refs:
- `tests/test_subscription_billing.py tests/test_credit_payments.py`: 23 passed, 13 subtests passed.
- Credit/admin/station focused regression: 100 passed, 13 subtests passed before the final rollover-only change; final targeted admin/station regression remained green.
- Full Python checkpoint during Task 2: 450 passed, 4 skipped, only the three recorded baseline video failures.
- Blocked on: No Task 3 blocker. Real MySQL integration remains unavailable with the configured local credentials.
- Next step: Dispatch Task 3 implementer; preserve legacy monthly first, remove the monthly gate from valid legacy traffic packs, then debit wallet batches before scalar fallback.

## Task 2 Drift Check

- Scope status: Inside approved catalog/payment/schema/admin-test scope; `app/admin.py` and station settlement received minimal necessary changes to collapse confirmation side-effect ownership.
- Compatibility status: Historical product metadata and active monthly entitlement execution remain; no old pending order is guessed or auto-confirmed.
- Retirement status: Public monthly/add-on sales and payment-confirmation proration paths are retired. Legacy monthly/add-on apply helpers remain unreachable from public create/confirm and bounded to compatibility/admin code.
- New risk signals:
- Registered catalog versions must remain available until no pending order references them.
- Advisory decision: continue; Task 3 must not recompute frozen payment promises or reintroduce scalar writes.

## Task 3 Completion Update

- Current todo: Task 4: add dry-run-first legacy credit migration tooling.
- Active slice: Deterministic migration planning, zero-drift reporting, and guarded transactional apply.
- Completed todos:
- Billing availability and debit order is active monthly, valid legacy traffic pack, permanent wallet, then scalar fallback/debt.
- Valid legacy traffic packs are spendable without an active monthly subscription.
- Video jobs persist exact wallet allocations and refund all sources exactly once after terminal job locking and full reference validation.
- Subscription precheck uses a pure period projection so insufficient debits do not mutate ORM state.
- Task 3 specification review passed after concurrent job locking, strict all-source refund validation, and DDL parity fixes.
- Task 3 code-quality review approved after strict wallet metadata validation, charged-total reconciliation, and frozen billable SKU support.
- Evidence refs:
- `tests/test_credit_wallet.py tests/test_subscription_billing.py tests/test_video_jobs.py`: 52 passed, 3 known baseline failures, 16 subtests passed.
- The three failures remain exactly the recorded `RequestLog.effective_cache_creation_input_per_million` constructor mismatch.
- Blocked on: No Task 4 code blocker. Real MySQL multi-connection verification remains unavailable.
- Next step: Build deterministic dry-run plans for positive scalar balance and valid traffic packs; never run production `--apply`.

## Task 3 Drift Check

- Scope status: Inside approved billing/video/schema/test scope; wallet strict refund API was extended to preserve the canonical allocation owner.
- Compatibility status: Active paid monthly periods, valid legacy packs, and scalar fallback remain spendable; old video jobs retain legacy refund compatibility.
- Retirement status: The monthly gate on legacy pack spending is retired. Pack/scalar compatibility owners remain until migration evidence and apply.
- New risk signals:
- `_credit_wallet_cents` is a temporary serializer bridge scheduled for explicit caller cleanup in Task 5.
- No real InnoDB multi-session evidence exists for terminal job/refund locking.
- Advisory decision: continue; migration tooling must prevent migrated legacy sources from being counted twice.
