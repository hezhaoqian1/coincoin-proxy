# Pure credit pack billing implementation - Evidence

Evidence is recorded below.

## EvidenceBundleDraft

- Artifact key: baseline-python
- Type: test
- Source: python -m pytest -q
- Summary: 426 passed, 3 failed, 4 skipped; failures are pre-existing video RequestLog constructor mismatch.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: task-5-credit-ui-admin
- Type: test-and-review
- Source: focused admin/payment/subscription suites, Node 20 Vite build, independent specification and code-quality reviews
- Summary: 108 focused Python tests and 13 subtests passed; the three-product permanent-credit UI, explicit wallet payloads, monthly rollover projection, fixed query counts, bounded recent-pack history, and radio keyboard behavior were approved with no remaining findings.
- Verifier: Codex root agent and independent reviewers

## EvidenceBundleDraft

- Artifact key: task-6-retirement-scan
- Type: source-scan
- Source: `rg` scan for proration, upgrade, monthly/add-on IDs, and removed helper names
- Summary: Monthly/add-on purchase execution, proration quote, and sales serializer actions have no remaining references. Remaining product IDs are limited to historical metadata and admin correction controls for active legacy records.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: final-python-regression
- Type: test
- Source: `python -m pytest -q`
- Summary: 524 passed, 4 skipped, 162 subtests passed; exactly three failures remain, all the recorded `RequestLog.effective_cache_creation_input_per_million` video baseline mismatch.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: pre-landing-payment-refund-boundaries
- Type: test-and-review
- Source: focused payment/admin/video regression tests plus independent pre-landing review
- Summary: Payment confirmation and admin replay compatibility balances now equal total available value. Failed video subscription debits from rolled-over, expired, disabled, or shortened-paid-until periods restore value as permanent credit without decrementing current-period usage. Seven focused regressions passed, including current-period usage greater than the old debit, expired rows not yet normalized, and a future `period_end` with an already elapsed `paid_until`.
- Verifier: Codex root agent and independent reviewer

## EvidenceBundleDraft

- Artifact key: final-frontend-go-static
- Type: build-and-test
- Source: Node 20 Vite production build, `go test ./...`, admin inline JavaScript `node --check`, and Python `py_compile`
- Summary: All commands passed. Frontend transformed 85 modules; only the existing >500 kB chunk advisory remains.
- Verifier: Codex root agent

## Final Evidence Limitations

- No production migration `--apply`, deployment, pending-order confirmation, or live database mutation was executed.
- Real MySQL/InnoDB savepoint, lock, window-query, and multi-connection behavior remains unverified locally.
- The three unrelated video RequestLog constructor failures remain an explicit baseline defect.

## EvidenceBundleDraft

- Artifact key: task-3-wallet-usage-video
- Type: test
- Source: `pytest tests/test_credit_wallet.py tests/test_subscription_billing.py tests/test_video_jobs.py -q`
- Summary: 52 passed, 16 subtests passed, with exactly 3 known baseline video failures caused by the pre-existing RequestLog constructor mismatch.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: task-3-review
- Type: review
- Source: independent specification and code-quality reviewer agents
- Summary: Specification compliant and code-quality approved after pure subscription projection, stable four-source debit ordering, terminal VideoJob row locking, strict allocation/reference validation, charged-total reconciliation, and frozen refund SKU support.
- Verifier: Codex root agent

## Task 3 Evidence Limitation

- Real MySQL multi-connection behavior for user/subscription/pack/wallet/job locks was not exercised because the local configured credentials are rejected. Current evidence is SQL shape, canonical-row refresh behavior, stale-view simulations, and object-level mutation assertions.

## EvidenceBundleDraft

- Artifact key: task-4-migration
- Type: test
- Source: `pytest tests/test_credit_migration.py tests/test_credit_wallet.py -q`
- Summary: 51 passed, 2 warnings, 10 subtests passed; covers deterministic planning, independent accounting buckets, conflicts, dirty-session refusal, locked fingerprint recheck, structured apply failure, post-commit reconciliation, explicit safety limits, and CLI output.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: task-4-cli
- Type: command
- Source: `scripts/migrate_legacy_credits.py --help` and no-database `--apply --json` safety refusal
- Summary: Help works without database configuration; apply without explicit limits refuses before opening the database and exits nonzero with structured JSON.
- Verifier: implementer and independent reviewer

## EvidenceBundleDraft

- Artifact key: task-4-review
- Type: review
- Source: independent specification and code-quality reviewer agents
- Summary: Specification compliant and code-quality approved after real SQLAlchemy identity/no-autoflush tests, independent zero-drift accounting, orphan/migrated-source integrity checks, explicit lock limits, and empty-plan transaction release.
- Verifier: Codex root agent

## Task 4 Evidence Limitation

- No migration `--apply` was run against production or a real MySQL replica. InnoDB lock scope, savepoints, commit-unknown behavior, and production row counts remain an operator release gate.

## EvidenceBundleDraft

- Artifact key: baseline-plan
- Type: plan
- Source: docs/aegis/plans/2026-07-12-pure-credit-pack-billing.md
- Summary: Executable six-task implementation plan with compatibility and retirement boundaries.
- Verifier: Aegis workspace check

## EvidenceBundleDraft

- Artifact key: task-1-wallet-tests
- Type: test
- Source: `pytest tests/test_credit_wallet.py -q`
- Summary: 17 passed, 2 warnings, 7 subtests passed; covers source-idempotent grants, savepoint conflict recovery, FIFO debit, exact refunds, schema parity, startup reruns, lock ordering, index error classification, UTC normalization, and financial constraints.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: task-1-adjacent-regression
- Type: test
- Source: `pytest tests/test_credit_wallet.py tests/test_subscription_billing.py -q`
- Summary: 26 passed, 2 warnings, 7 subtests passed.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: task-1-review
- Type: review
- Source: independent specification and code-quality reviewer agents
- Summary: Specification compliant and code-quality approved after fixes for concurrent grants, schema parity, consistent balance lock ordering, fail-closed unique-index migration, UTC-naive timestamps, and persistent financial constraints.
- Verifier: Codex root agent

## Evidence Limitation

- No live MySQL multi-connection integration test was run. The configured local `test:test` credentials were rejected, so InnoDB savepoint/row-lock behavior, driver errno shapes, and CHECK enforcement remain covered by MySQL dialect compilation and focused simulations rather than a real database.

## EvidenceBundleDraft

- Artifact key: task-2-credit-catalog-payment
- Type: test
- Source: `pytest tests/test_subscription_billing.py tests/test_credit_payments.py -q`
- Summary: 23 passed, 13 subtests passed; covers the three public credit products, exact RMB validation, frozen order fields, historical catalog-version confirmation, single-batch grants, legacy pending quarantine, replay idempotency, and common station ownership.
- Verifier: Codex root agent

## EvidenceBundleDraft

- Artifact key: task-2-expanded-regression
- Type: test
- Source: focused credit payment, admin payment, wallet, referral, webhook, and station tests
- Summary: Focused expanded suites remained green; the implementation checkpoint reached 100 passed and 13 subtests before the final isolated version-rollover test, with subsequent targeted reruns green.
- Verifier: implementer and Codex root agent

## EvidenceBundleDraft

- Artifact key: task-2-full-python-checkpoint
- Type: test
- Source: `python -m pytest -q`
- Summary: 450 passed, 4 skipped, 3 failed; the failures are exactly the recorded baseline video RequestLog constructor mismatch.
- Verifier: Task 2 implementer

## EvidenceBundleDraft

- Artifact key: task-2-review
- Type: review
- Source: independent specification and code-quality reviewer agents
- Summary: Specification compliant and code-quality approved after consolidating versioned catalog ownership, fail-closed legacy pending handling, station side effects, confirmed replay compensation, and admin frozen-order audit fields.
- Verifier: Codex root agent
