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
