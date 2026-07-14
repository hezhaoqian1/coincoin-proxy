# Service Reliability Center - Evidence

No evidence has been recorded yet.

## EvidenceBundleDraft

- Artifact key: task1-focused-tests
- Type: test
- Source: pytest tests/test_reliability.py tests/test_channel_monitoring.py tests/test_monitoring_probes.py tests/test_admin_usage_fields.py
- Summary: 99 tests passed; cached reliability overview, aggregation, authentication, and provider-channel baseline are green.
- Verifier: Codex local execution

## EvidenceBundleDraft

- Artifact key: task2-reconcile-tests
- Type: test
- Source: pytest tests/test_channel_monitoring.py tests/test_reliability.py tests/test_admin_usage_fields.py tests/test_monitoring_probes.py
- Summary: 104 tests passed; route-derived auto monitors are capped, reuse manual coverage, disable derived state safely, and channel CRUD remains green.
- Verifier: Codex local execution

## EvidenceBundleDraft

- Artifact key: task3-browser-ui
- Type: browser
- Source: gstack browse at 1440x900 and 390x844 against a local fixture server
- Summary: The reliability page rendered without console errors or page-level overflow; route details and explicit probe action worked; network logs contained overview polling and the clicked monitor run only, with no `/probes/*` or legacy monitoring snapshot request.
- Verifier: Codex local execution

## EvidenceBundleDraft

- Artifact key: task4-compat-review
- Type: test-and-review
- Source: pytest focused compatibility suites, py_compile, node --check, hot-path grep, git diff review
- Summary: 243 tests passed; Python and JavaScript syntax checks passed; no reliability import exists in request hot-path modules; review fixes cover rollback safety, unsupported endpoint false probes, worst-monitor actions, and monitored-channel deletion.
- Verifier: Codex local execution
