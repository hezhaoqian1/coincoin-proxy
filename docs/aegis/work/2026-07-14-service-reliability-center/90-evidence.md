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
