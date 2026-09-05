# Enterprise Reporting API - Evidence

## EvidenceBundleDraft

- Artifact key: baseline-suite
- Type: test
- Source: /tmp/coincoin-enterprise-baseline.log
- Summary: Pre-change baseline: 431 tests, 3 existing video_jobs errors caused by RequestLog missing effective_cache_creation_input_per_million; 1 skipped.
- Verifier: unittest

## EvidenceBundleDraft

- Artifact key: persistence-import
- Type: test
- Source: python import app.models enterprise classes
- Summary: All three enterprise ORM models import and map successfully.
- Verifier: python

## EvidenceBundleDraft

- Artifact key: focused-regression
- Type: test
- Source: /tmp/coincoin-enterprise-regression.log
- Summary: Enterprise, existing auth, and admin regression: 106 tests passed.
- Verifier: unittest

## EvidenceBundleDraft

- Artifact key: admin-ui-browser
- Type: screenshot
- Source: /tmp/coincoin-enterprise-desktop.png,/tmp/coincoin-enterprise-edit-desktop.png,/tmp/coincoin-enterprise-mobile.png,/tmp/coincoin-enterprise-edit-mobile.png
- Summary: Desktop and 390px mobile enterprise list/modal are nonblank; no page or account-row overflow, no row overlap, modal footer remains visible.
- Verifier: gstack-browse

## EvidenceBundleDraft

- Artifact key: docs-contract
- Type: documentation
- Source: docs/reference/enterprise-reporting-api.md,docs/operations/enterprise-reporting.md,docs/releases/2026-07-enterprise-reporting-api.md
- Summary: Reference, runbook, release note, placeholders, config, rotation, and rollback guidance are present.
- Verifier: shell

## EvidenceBundleDraft

- Artifact key: final-focused-regression
- Type: test
- Source: python -m unittest tests.test_enterprise_reporting tests.test_proxy_auth_cache tests.test_admin_usage_fields
- Summary: Fresh affected regression: 112 tests passed, including 32 enterprise reporting tests, model-auth rejection, and all management API success paths.
- Verifier: unittest

## EvidenceBundleDraft

- Artifact key: final-full-suite
- Type: test
- Source: python -m unittest discover -s tests -p 'test_*.py'
- Summary: Fresh full suite: 463 tests ran; the same 3 baseline video_jobs errors remained, 1 test skipped, and no new failure appeared.
- Verifier: unittest

## EvidenceBundleDraft

- Artifact key: final-admin-ui-mobile
- Type: screenshot
- Source: /tmp/coincoin-enterprise-table-mobile-final.png
- Summary: At 390x844, body width stayed 390px, the 356px table container scrolled its 760px table by 404px, and the rightmost action header was fully visible.
- Verifier: gstack-browse

## EvidenceBundleDraft

- Artifact key: final-docs-static-security
- Type: verification
- Source: git diff --check; per-file docs checker; Node inline-script parse; Python import; source scan
- Summary: Diff whitespace, six new managed docs, inline admin JavaScript, app/model imports, and customer/credential boundary scans passed; full repository docs harness remains unavailable in this isolated baseline.
- Verifier: shell
