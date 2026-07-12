# Pure credit pack billing implementation - Evidence

No evidence has been recorded yet.

## EvidenceBundleDraft

- Artifact key: baseline-python
- Type: test
- Source: python -m pytest -q
- Summary: 426 passed, 3 failed, 4 skipped; failures are pre-existing video RequestLog constructor mismatch.
- Verifier: Codex root agent

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
