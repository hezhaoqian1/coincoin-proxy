# Channel Representative Probe - Evidence

## Design and Plan

- Commit: `a41e014`.
- Outcome: approved design and implementation plan define one representative probe per channel, exact admin override/reset, channel-only probe health, observation-only routing boundaries, compatibility retention, and production acceptance requirements.

## Task 1: Representative selection and single-probe execution

- Commits: `3f2a8ee`, `41d4d7b`.
- Focused verification: `16 passed, 2 warnings` in `tests/test_channel_monitoring.py`.
- Spec compliance: approved after rejecting non-2xx probe responses.
- Code quality: approved.
- Observed boundary: one active monitor per channel, one real generation POST, no `/models` preflight, no routing mutation.

## Task 2: Channel monitor-selection API and payload

- Commits: `72e1a4b`, `bb8bd40`, `782ec2f`, `3b5b3a0`, `41b1ce7`.
- Focused verification: `118 passed, 42 warnings` across channel monitoring, admin usage fields, and reliability tests.
- Spec compliance: approved after reconciled readback and missing/invalid selection coverage.
- Code quality: approved after single-transaction ownership, database row locking, target identity preservation, consistent payloads, and valid-target ranking.
- Observed boundary: admin selection reconciles monitoring only and does not refresh or mutate request routing.

## Task 3: Channel-first reliability semantics and UI

- Commits: `b93e7fe`, `db2a4fb`, `a9e4034`, `9a925de`, `d86e747`, `f562b34`.
- Focused verification: reliability/admin/channel suites passed throughout; latest persistence-focused slice reported `36 passed` and the full reliability module reported `22 passed` before widening.
- Spec compliance: approved after real-latency health and invalid-manual reset-to-auto UI support, which requires operator action and does not automatically replace an invalid manual selection.
- Code quality: approved after fallback-source attribution, endpoint isolation/normalization, bounded fallback-rate math, image alias mapping, and compatibility-preserving fallback source persistence widening.
- Observed boundary: representative probe status affects channel health only; public-model health uses route coverage, real traffic, fallback source attribution, latency, and router cooldown.

## Task 4: Migration compatibility and architecture sync

- Documentation commit: this Task 4 commit, `docs: align reliability channel probe architecture`.
- Architecture action: ADR-0002 amended in place and the service-reliability baseline updated to the implemented channel representative-probe owner model.
- Compatibility suite: `283 passed, 89 warnings` in 10.04 seconds across `tests/test_reliability.py`, `tests/test_channel_monitoring.py`, `tests/test_monitoring_probes.py`, `tests/test_admin_usage_fields.py`, `tests/test_openai_compat_defaults.py`, and `tests/test_anthropic_compat.py`.
- JavaScript syntax: `node --check app/static/admin_assets/service-reliability.js` passed.
- Python syntax: `py_compile` passed for `app/admin.py`, `app/channel_monitoring.py`, `app/main.py`, `app/models.py`, `app/reliability.py`, `app/schemas.py`, and `app/usage_buffer.py`.
- Aegis workspace: helper help was inspected; the first read-only check rejected the unsupported `continue-task-5` drift enum, the record was corrected to advisory `continue`, `bundle` generated `gate-input-pack.json` and `proof-bundle.md`, and the final workspace check passed.
- Migration compatibility: `fallback_from_channel_id` is widened to 512 in the ORM model, create-table DDL, startup migration, and buffered persistence truncation. The application performs no data `UPDATE` or `DELETE` and preserves existing values; this evidence makes no claim that MySQL avoids an internal table rebuild or row rewrite while applying the DDL.
- Retirement outcome: `extra_models` remains persisted and exposed for compatibility but representative probes execute only `primary_model`; reconciliation disables redundant automatic monitors, clears executable extras, and retains monitor history.
- Review outcome: Tasks 1-3 received recorded spec-compliance and code-quality approvals; Task 4 records were checked against the implemented branch and fresh local compatibility/static evidence.
- Additional docs-validator gap: the repository guidance names `tests.test_docs_check` and `scripts/check_docs.py`, but neither file exists in this checkout; the unittest command therefore failed at import and no script command was available.
- Remaining boundary: merge, deployment, browser/admin workflow checks, one healthy production probe, and production routing non-mutation confirmation remain Task 5 work.

## EvidenceBundleDraft

- Artifact key: task4-compatibility
- Type: test-static-and-workspace-validation
- Source: pytest six-module compatibility suite; node --check; Python py_compile; aegis-workspace.py check/bundle/check
- Summary: 283 tests passed with 89 warnings; JavaScript and seven touched Python modules passed syntax checks; Aegis bundle generation and the final read-only workspace check passed after correcting the drift decision to the supported advisory enum.
- Verifier: Codex local execution on codex/channel-probe-model

## Task 5: Pre-ship Review and Local Acceptance

- Hardening commits: `05260d9`, `ef81cc2`.
- Integrated review: the first full-branch review found monitor ownership, control-plane locking, explicit lease, endpoint mapping, and required-width migration gaps; regression fixes were applied and the final independent result was `Pre-Landing Review: No issues found.`
- Fresh compatibility suite: `303 passed, 89 warnings` across reliability, channel monitoring, monitor API, protected monitoring probes, admin usage, OpenAI compatibility, and Anthropic compatibility tests.
- Static checks: service reliability JavaScript syntax, touched Python module compilation, `git diff --check`, and clean worktree checks passed.
- Public-diff credential scan: zero high, medium, low, or warning findings.
- Local browser acceptance: desktop and mobile layouts rendered; channel-first section order, stable horizontal table scrolling, representative model/endpoint/mode display, valid and invalid monitor-selection states, operator reset-to-auto, and explicit monitor-run POST behavior were exercised with mocked read-side payloads and no post-mock console errors.
- Known baseline issue: the broader suite retains three pre-existing video-job failures unrelated to this branch; the focused compatibility and migration contracts pass.
- Remaining boundary: push/merge, Railway deployment, production browser/API checks, one healthy production probe, and production routing non-mutation confirmation.
