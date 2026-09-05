# Admin Alert Center - Evidence

Release evidence is recorded below.

## EvidenceBundleDraft

- Artifact key: runtime-policy-alert-history-red-green
- Type: pytest
- Source: tests/test_fallback_alerts.py
- Summary: Focused suite passed 17 tests after runtime policy, bounded scheduling, and sanitized pending/sent/failed event lifecycle implementation.
- Verifier: pytest tests/test_fallback_alerts.py -q

## EvidenceBundleDraft

- Artifact key: alert-admin-api-red-green
- Type: pytest
- Source: tests/test_alert_admin.py
- Summary: Protected alert config, validation, test-send, and capped history API suite passed with fallback regression: 25 tests.
- Verifier: pytest tests/test_alert_admin.py tests/test_fallback_alerts.py -q

## EvidenceBundleDraft

- Artifact key: alert-ui-focused-regression
- Type: pytest
- Source: tests/test_admin_usage_fields.py and focused backend suites
- Summary: Service Reliability alert controls/history static contract and focused regression suites passed: 164 tests; JavaScript syntax and diff whitespace checks passed.
- Verifier: pytest four focused test files; node --check service-reliability.js; git diff --check

## EvidenceBundleDraft

- Artifact key: prelanding-review-fixes
- Type: review-and-pytest
- Source: independent prelanding review plus focused regression
- Summary: Four review findings were fixed: single-slot sender starvation, unbounded audit persistence delay, same-second replica refresh miss, and polling index mismatch. Focused suite passed 171 tests after fixes.
- Verifier: pytest tests/test_fallback_alerts.py tests/test_alert_admin.py tests/test_admin_usage_fields.py tests/test_reliability.py -q

## EvidenceBundleDraft

- Artifact key: full-suite-baseline-differential
- Type: pytest
- Source: full repository suite and origin/master differential
- Summary: Full suite after review fixes: 686 passed, 5 skipped, 166 subtests passed, with only the same 3 pre-existing video RequestLog field failures; video paths are unchanged from origin/master.
- Verifier: pytest -q; git diff --exit-code origin/master -- app/video_jobs.py tests/test_video_jobs.py

## EvidenceBundleDraft

- Artifact key: final-pre-ship-verification
- Type: verification
- Source: focused tests, full-suite differential, static checks, independent re-review
- Summary: Final focused suite passed 173 tests; JS syntax, Python compileall, both diff checks, secret scan, MySQL UPSERT compile, runtime probes, and independent re-review passed. Full suite remained 686 passed with only 3 base-branch video failures.
- Verifier: pytest focused files; pytest -q; node --check; compileall; git diff --check; independent prelanding re-review

## EvidenceBundleDraft

- Artifact key: merged-production-deployment
- Type: release-verification
- Source: GitHub PR #17, GitHub deployment statuses, public and protected production endpoints
- Summary: PR #17 squash-merged as `eea795a2edeeac40f7c4cb2932d06e4d585cee1f`; both Railway production deployments succeeded; public health and protected alert configuration checks returned HTTP 200.
- Verifier: GitHub PR/deployment APIs; GET https://coincoin.ai/health; GET /admin/alerts/config with the administrator credential
