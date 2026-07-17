# CoinCoin Admin Performance Optimization - Evidence

No evidence has been recorded yet.

## EvidenceBundleDraft

- Artifact key: batch-user-billing-red-green
- Type: test
- Source: tests.test_admin_usage_fields targeted unittest run
- Summary: New tests failed before implementation, then passed. On latest master, the existing credit-aware batch owner now uses three fixed billing queries (subscription, traffic packs, permanent credits), removes the window function and separate active-pack query, and list_users adds one paged user query with limit/offset.
- Verifier: unittest: 4 tests OK

## EvidenceBundleDraft

- Artifact key: combined-leaderboards-red-green
- Type: test
- Source: tests.test_admin_usage_fields targeted unittest run
- Summary: Batch leaderboard endpoint and UI wiring tests failed before implementation and then passed; 1h, 4h, and 24h payloads are returned from one SQL execution.
- Verifier: unittest: 3 tests OK

## EvidenceBundleDraft

- Artifact key: final-targeted-regression
- Type: test
- Source: python -m unittest -q tests.test_admin_usage_fields tests.test_admin_timing tests.test_subscription_billing tests.test_quota_lifecycle
- Summary: Fresh latest-master ship-gate run completed 128 targeted tests with OK. It covers admin payload compatibility, credit-aware bulk and single-user billing equivalence, pagination boundaries, finance summary, leaderboard metrics and invalid inputs, analytics cache warm/TTL/error recovery, provider totals and China-midnight windows, timing middleware, subscription billing, and quota lifecycle.
- Verifier: unittest exit 0

## EvidenceBundleDraft

- Artifact key: full-suite-baseline-differential
- Type: regression
- Source: full unittest discovery on task worktree plus tests.test_video_jobs on clean detached HEAD worktree
- Summary: Latest-master full discovery ran 638 tests: 3 errors and 2 skips. The same three video-generation RequestLog constructor errors reproduce on clean origin/master 9f8147a (27 video tests, 3 errors), and app/video_jobs.py plus app/models.py are unchanged by this task; classified as an independent baseline failure.
- Verifier: unittest differential reproduction

## EvidenceBundleDraft

- Artifact key: final-static-verification
- Type: static-check
- Source: py_compile, Node inline-script parse, git diff --check
- Summary: Changed Python modules compile, both inline admin scripts parse, and git diff --check reports no whitespace errors. The latest-master coincoin-web production Vite build completed successfully with 87 modules transformed by temporarily reusing the primary workspace's matching node_modules; coincoin-web source was not modified.
- Verifier: all available static checks exit 0

## EvidenceBundleDraft

- Artifact key: performance-shape
- Type: performance
- Source: query-count and cache-call tests in tests.test_admin_usage_fields
- Summary: The original diagnosis found up to roughly 601 user-page statements on the older branch. Latest master had already introduced a five-query fixed batch path; this integration reduces it to four total statements (one paged user query plus subscription, traffic-pack, and permanent-credit queries), while removing the window-function dependency. Three leaderboard requests become one request/query; concurrent cold dashboard builds single-flight behind a 300-second cache; provider all-time totals leave the warm path behind a 900-second cache.
- Verifier: targeted tests and final diff inspection

## EvidenceBundleDraft

- Artifact key: provider-midnight-window-regression
- Type: regression
- Source: tests.test_admin_usage_fields.AdminUsageFieldTests.test_provider_channel_historical_totals_are_cached_between_page_loads
- Summary: Pre-landing review found that a today_start lower bound truncated 1h/4h channel statistics just after China midnight. The query now starts at min(today_start, since_4h), and a frozen 01:30 China-time regression asserts the four-hour boundary is included.
- Verifier: focused unittest exit 0 and 100-test ship regression exit 0

## EvidenceBundleDraft

- Artifact key: ship-coverage-and-build
- Type: ship-gate
- Source: gstack coverage audit, 100-test targeted suite, and Vite production build
- Summary: Coverage audit improved from 60% to the 80% target after seven boundary tests. The latest-master targeted suite ran 128 tests with OK. Vite 6.4.3 transformed 87 modules and completed a production build with Node 24.14.0; the existing 674 kB chunk-size warning remains informational.
- Verifier: coverage gate PASS 80%; unittest exit 0; vite build exit 0

## EvidenceBundleDraft

- Artifact key: master-integration
- Type: integration
- Source: cherry-pick onto origin/master 9f8147a with conflict-aware owner reconciliation
- Summary: Latest master already owned credit-aware admin batch billing. Integration retained that canonical owner, preserved permanent-credit fields plus image keepalive and reliability routes, collapsed active/recent traffic-pack reads into one MySQL-compatible query, and avoided introducing a duplicate billing owner. The merged admin/timing suite ran 114 tests with OK before documentation integration.
- Verifier: manual conflict review, py_compile, and unittest exit 0

## EvidenceBundleDraft

- Artifact key: final-master-ship-gate
- Type: ship-gate
- Source: origin/master-integrated targeted/full unittest runs, clean-master differential, static checks, and Vite build
- Summary: On origin/master 9f8147a plus this branch, 128 targeted tests pass, Python and inline scripts parse, Aegis and diff checks pass, and Vite transforms 87 modules successfully. Full discovery runs 638 tests with only the same three video RequestLog errors reproduced by 27 clean-master video tests. Middleware order coverage was updated and passes.
- Verifier: targeted/build/static exit 0; full-suite differential confirms no in-branch failures
