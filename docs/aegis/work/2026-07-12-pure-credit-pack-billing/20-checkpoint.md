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

## Task 4 Completion Update

- Current todo: Task 5: update admin/customer payloads and recharge UI.
- Active slice: Explicit wallet totals, three-product purchase experience, and removal of the serializer compatibility bridge.
- Completed todos:
- Added a default dry-run legacy credit migration CLI with integer-cent accounting and stable JSON/human reports.
- Positive scalar balance and valid traffic packs have deterministic permanent-credit plans; negative debt is retained and reported.
- Apply is guarded by explicit scan/plan limits, locked fingerprint recheck, all-or-nothing retirement/grant, structured failure states, and post-commit reconciliation.
- No production apply, live mutation, or pending-order action was executed.
- Task 4 specification review passed after independent accounting, orphan/migrated-source checks, no-autoflush and identity refresh fixes.
- Task 4 code-quality review approved after empty-plan lock release and explicit full-batch safety limits.
- Evidence refs:
- `tests/test_credit_migration.py tests/test_credit_wallet.py`: 51 passed, 10 subtests passed.
- `scripts/migrate_legacy_credits.py --help`: passed without database configuration.
- Blocked on: No Task 5 blocker. Real MySQL/InnoDB migration behavior remains unverified and production apply is still unauthorized.
- Next step: Remove `_credit_wallet_cents` bridge by passing explicit snapshot totals, update payloads, and replace Recharge UI with three permanent-credit cards.

## Task 4 Drift Check

- Scope status: Inside approved migration script/test boundary; no runtime schema or billing path changed.
- Compatibility status: Migration apply would retire only the exact migrated source in the same transaction; dry-run leaves all state untouched.
- Retirement status: Tooling is ready, but traffic-pack/scalar owners remain active because no live apply was run.
- New risk signals:
- The full-batch apply locks every scanned legacy row; explicit operator limits are mandatory but real production sizing is unknown.
- Advisory decision: continue to UI/payload work; production migration remains a separate approval and evidence gate.

## Task 5 Completion Update

- Current todo: Task 6: retire stale sales logic and run final regression.
- Active slice: Customer/admin payloads, permanent-credit Recharge UI, and fixed-query finance projection.
- Completed todos:
- Removed the `_credit_wallet_cents` serializer bridge; every production caller passes wallet cents explicitly.
- Added canonical `credit_wallet`, total available fields, and bounded `credit_balance` compatibility aliases.
- Recharge and the landing page expose only the three permanent-credit products.
- Existing active monthly and usable historical traffic-pack entitlements are read-only on the customer page.
- Admin user and finance lists use fixed-count batch queries, reproduce monthly rollover semantics, and retain active packs plus at most 50 recent historical packs per user.
- Task 5 specification and code-quality reviews approved after rollover, N+1, bounded-history, and radio-keyboard fixes.
- Evidence refs:
- `tests/test_admin_usage_fields.py tests/test_credit_payments.py tests/test_subscription_billing.py`: 108 passed, 13 subtests passed before Task 6 retirement deletion.
- Node 20 production build: 85 modules transformed; only the existing chunk-size advisory.
- Blocked on: No Task 6 code blocker. Real MySQL window-query and multi-connection behavior remain unverified locally.
- Next step: Delete unreachable monthly/add-on purchase helpers, update documentation/admin copy, and run full regression.

## Task 5 Drift Check

- Scope status: Inside approved admin/customer payload and UI boundary.
- Compatibility status: Active old monthly and usable historical packs remain visible and spendable; no old product becomes purchasable.
- Retirement status: Public monthly/add-on UI and serializer sales actions are gone; permanent wallet is canonical for new purchases.
- New risk signals:
- Admin batch history limiting relies on the project's MySQL 8 window-function baseline.
- Advisory decision: continue to final retirement and regression.

## Task 6 Completion Candidate

- Current todo: Final verification, workspace proof bundle, architecture decision check, and branch handoff.
- Active slice: Retirement evidence and final regression.
- Completed todos:
- Deleted unreachable monthly start/renew/reset/upgrade and traffic-pack purchase execution helpers.
- Deleted legacy proration quote and generic legacy sales serialization branches.
- Removed stale customer CSS and changed public landing/documentation language to permanent 美金额度.
- Updated admin displays to include permanent wallet totals and to state that historical packs are usable without a monthly gate.
- Classified remaining legacy product IDs as required active-record metadata and admin compatibility controls.
- Updated stale test fakes to model the billing snapshot boundary explicitly instead of returning User rows for every query.
- Evidence refs:
- Full Python: 518 passed, 4 skipped, exactly 3 recorded baseline video failures; 162 subtests passed.
- Frontend Node 20 build: 85 modules transformed and built successfully.
- `usage-quota-service`: `go test ./...` passed.
- Admin inline JavaScript syntax check and Python compilation passed.
- Blocked on: No code blocker. Production migration apply, live deployment, real MySQL concurrency/window integration, and the unrelated video baseline defect remain outside this completion candidate.
- Next step: Independent final review, Aegis workspace/ADR checks, then commit documentation and retirement changes.

## Task 6 Drift Check

- Scope status: Inside approved retirement, documentation, and regression boundary.
- Compatibility status: Paid monthly periods, valid historical packs, historical product metadata, and admin correction controls remain.
- Retirement status: Old public sales, proration, renew/reset/upgrade execution, and monthly-gated add-on purchase logic are deleted.
- New risk signals:
- Live migration has not run, so positive scalar balance and traffic-pack compatibility owners remain active.
- Real MySQL/InnoDB and assistive-technology browser integration are not covered by local evidence.
- Advisory decision: needs final review and proof-bundle checks; no scope drift detected.
