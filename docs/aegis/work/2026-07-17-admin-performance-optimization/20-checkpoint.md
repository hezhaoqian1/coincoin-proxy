# CoinCoin Admin Performance Optimization - Checkpoint

- Task ID: 2026-07-17-admin-performance-optimization
- Current todo: Execute Task 1 batch billing and user pagination with RED/GREEN tests.
- Active slice: Task 1: batch user billing and page the user UI.
- Blocked on: none
- Next step: Add failing query-count and billing-equivalence tests.

## Checkpoint Update

- Current todo: Implement one-query combined dashboard leaderboards.
- Active slice: Task 2: combine dashboard leaderboards.
- Completed todos:
- Batch user billing and add 50-row admin UI pagination.
- Evidence refs:
- batch-user-billing-red-green
- Blocked on: none
- Next step: Add a failing batch leaderboard endpoint/query-count test and UI wiring test.

## DriftCheckDraft

- Scope status: Inside admin read-path scope.
- Compatibility status: Billing payload and single-user write/detail paths preserved.
- Retirement status: Per-user list billing calls retired; detail/mutation calls retained intentionally.
- New risk signals:
- none
- Advisory decision: continue

## Checkpoint Update

- Current todo: Protect operating analytics cold cache and bound provider-channel scans.
- Active slice: Task 3: analytics single-flight cache and provider historical-total cache.
- Completed todos:
- Batch user billing and pagination.
- Combine dashboard leaderboards into one query and request.
- Evidence refs:
- batch-user-billing-red-green
- combined-leaderboards-red-green
- Blocked on: none
- Next step: Add failing concurrent analytics cache and provider warm-cache tests.

## DriftCheckDraft

- Scope status: Inside approved admin read-path scope.
- Compatibility status: Old single-window endpoint retained; admin UI moved to the combined endpoint.
- Retirement status: Triple admin UI calls retired; compatibility endpoint retained with explicit trigger for future deletion.
- New risk signals:
- none
- Advisory decision: continue

## Checkpoint Update

- Current todo: Implementation and task-scoped verification complete; preserve branch for user-directed integration.
- Active slice: Task 5: integrated verification and handoff.
- Completed todos:
- Batch user billing and 50-row admin pagination.
- One-query 1h/4h/24h leaderboards.
- Analytics single-flight cache and provider historical-total cache.
- Admin-only timing headers and slow logs.
- Fresh targeted regression, static checks, full-suite differential, and final diff review.
- Evidence refs:
- final-targeted-regression
- full-suite-baseline-differential
- final-static-verification
- performance-shape
- Blocked on: none
- Next step: Await user direction for base alignment, commit, review, merge, or deployment.

## DriftCheckDraft

- Scope status: Aligned: admin read paths, UI wiring, observability, tests, and method records only.
- Compatibility status: Existing admin fields and old single-window endpoint remain; billing debit, payment, routing, model selection, and public APIs are unchanged.
- Retirement status: N+1 list billing calls, triple UI leaderboard calls, duplicate cold dashboard builds, and warm-path all-history channel scans are retired from the main admin path; compatibility endpoint retained intentionally.
- New risk signals:
- Bulk traffic-pack query count is fixed, but returned history rows can grow for users with unusually large pack histories.
- In-memory caches are per process and need production timing evidence for real latency gains.
- Feature branch is based on codex/documentation-engineering and is not aligned to current master.
- Advisory decision: continue

## DriftCheckDraft

- Scope status: Aligned after latest-master reconciliation: admin read performance, UI wiring, observability, tests, and records only.
- Compatibility status: Permanent credit-wallet fields, image keepalive middleware, reliability/channel monitor data, existing admin fields, billing debits, payments, routing, and public APIs are preserved.
- Retirement status: Latest master's canonical _admin_billing_states_batch remains sole owner; its separate active/recent pack queries and window-function dependency are retired. Triple leaderboard calls, duplicate cold dashboard builds, and warm-path all-history scans remain retired.
- New risk signals:
- Bulk traffic-pack query count is fixed, but returned history rows can grow for users with unusually large pack histories.
- In-memory caches are per process and require production timing evidence.
- Advisory decision: continue
