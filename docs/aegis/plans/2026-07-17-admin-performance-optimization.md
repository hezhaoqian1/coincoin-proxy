# CoinCoin Admin Performance Optimization Plan

## Goal

Remove the confirmed admin query fan-out and repeated raw-log scans while
preserving billing correctness, admin response fields, and public gateway
behavior.

## Architecture

- `app/billing.py` remains the canonical billing serializer and debit owner.
- `app/admin.py` retains the latest master branch's existing batch admin billing
  read owner and coordinates pagination, combined leaderboards, analytics cache
  protection, and provider statistics.
- `app/main.py` owns request-level timing middleware.
- `app/static/admin.html` consumes the optimized endpoints without changing the
  visible business meaning.

## Tech Stack

FastAPI, SQLAlchemy async MySQL, static HTML/JavaScript, Python unittest/httpx.

## Baseline/Authority Refs

- `docs/aegis/specs/2026-07-17-admin-performance-optimization-brief.md`
- `docs/aegis/plans/2026-05-13-operating-dashboard-v2.md`
- `app/admin.py`, `app/billing.py`, `app/main.py`, `app/static/admin.html`
- `tests/test_admin_usage_fields.py`

## Compatibility Boundary

No schema migration. No change to billing debit, payment, routing, model
selection, public APIs, or existing admin response fields. Statistics may have
the bounded freshness described in the approved brief.

## Verification

Use the project virtualenv from the primary workspace:

```bash
env PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/pycache \
  COINCOIN_DB_HOST=localhost COINCOIN_DB_NAME=test \
  COINCOIN_DB_USER=test COINCOIN_DB_PASSWORD=test \
  /Users/windupbird/Documents/Coincoin中转站/coincoin-proxy/.venv/bin/python \
  -m unittest tests.test_admin_usage_fields -v

git diff --check
```

Build the frontend with the existing Node installation after the backend tests.

## Change Necessity

The current latency is caused by source-level query fan-out and unbounded
aggregation. Configuration or documentation cannot remove those calls.
Decision: code-change. Minimum boundary: the existing admin billing batch read, admin read routes,
admin UI request wiring, request timing middleware, and their tests.

## Complexity and Owner Check

`app/admin.py`, `app/static/admin.html`, and the admin test module are already
over the Aegis 800-line pressure threshold. Integration with the latest master
reuses `_admin_billing_states_batch` instead of adding a second bulk owner.
Admin edits replace or wrap existing blocks rather than adding a parallel
analytics subsystem. No new persistence owner is introduced.

## Task 1: Batch user billing and page the user UI

Files: `app/admin.py`, `app/static/admin.html`,
`tests/test_admin_usage_fields.py`.

Repair track: optimize the existing master batch owner to one subscription
query, one traffic-pack query, and one permanent-credit query. Remove the
separate active/recent traffic-pack queries and the window-function dependency;
apply pagination to the user list while preserving finance summary reuse.

Retirement track: list endpoints must no longer call the single-user billing
owner in a loop. Detail/mutation endpoints keep it.

- [x] Add tests asserting billing payload equivalence and bounded query count.
- [x] Run the new tests and confirm RED.
- [x] Optimize the existing bulk billing loader, add user `limit`/`offset`, and wire the 50-row UI pager.
- [x] Run the new tests and confirm GREEN.
- [x] Run existing user, finance, subscription, and traffic-pack admin tests.

## Task 2: Combine dashboard leaderboards

Files: `app/admin.py`, `app/static/admin.html`,
`tests/test_admin_usage_fields.py`.

Repair track: calculate 1h, 4h, and 24h conditional aggregates in one query and
return the existing item fields grouped by window.

Retirement track: the admin UI stops calling the single-window compatibility
endpoint three times. The endpoint remains available until external-call
evidence permits deletion.

- [x] Add one-query and response-shape tests for the batch endpoint.
- [x] Run the new tests and confirm RED.
- [x] Implement the shared aggregate and switch the UI request.
- [x] Run the new tests and confirm GREEN.
- [x] Run existing leaderboard and admin UI wiring tests.

## Task 3: Protect analytics cold cache and bound provider scans

Files: `app/admin.py`, `tests/test_admin_usage_fields.py`.

Repair track: add per-period single-flight locking, extend operating-dashboard
TTL to 300 seconds, split provider recent and historical totals, and cache only
historical totals for 15 minutes.

Retirement track: duplicate cold recomputation and per-page all-history scans
must no longer be on the normal page path.

- [x] Add concurrent cache-build and warm-provider-cache tests.
- [x] Run the new tests and confirm RED.
- [x] Implement locks, TTLs, bounded recent query, and historical total cache.
- [x] Run the new tests and confirm GREEN.
- [x] Verify cache metadata and provider payload fields stay compatible.

## Task 4: Add admin request timing

Files: `app/admin_timing.py`, `app/main.py`, `tests/test_admin_timing.py`.

Repair track: expose server timing so production slow pages can be identified
without inspecting secrets.

Retirement track: no old owner is replaced; this is an additive observation
surface with no request-body logging.

- [x] Add header and slow-log tests.
- [x] Run the new tests and confirm RED.
- [x] Implement the isolated admin-only timing middleware and register it in `main.py`.
- [x] Run the new tests and confirm GREEN.
- [x] Verify non-admin response behavior is unchanged.

## Task 5: Integrated verification

- [x] Run `tests.test_admin_usage_fields` completely.
- [x] Run the closest billing and main-app regression tests.
- [x] Check the static admin inline JavaScript and run the `coincoin-web`
  production Vite build by temporarily reusing the primary workspace's matching
  `node_modules`; 86 modules transformed and the build completed successfully.
- [x] Run `git diff --check` and inspect the complete diff.
- [x] Record query-count and cache evidence in the work record.

## Risks and Rollback

- Bulk billing grouping must preserve the existing active-subscription rule and
  traffic-pack ordering. Roll back the list endpoints to the single-user helper
  if equivalence tests fail.
- The bulk traffic-pack query fixes query fan-out but still returns the selected
  page's historical pack rows before retaining the latest 50 per user. Admin UI
  pagination limits this to 50 users per page; monitor row counts if individual
  users accumulate unusually large traffic-pack histories.
- Combined leaderboard ranking occurs after one grouped query; verify each
  window independently against the existing endpoint semantics.
- In-memory caches are per process. They reduce normal-path load but are not a
  persistent rollup and are safe to clear by process restart.
- Timing middleware must never log query strings, authorization headers, request
  bodies, or response bodies.

## Execution Readiness View

- Intent Lock: improve admin read performance only.
- Scope Fence: no schema, deploy, production mutation, billing debit, payment,
  routing, or public API changes.
- Baseline Lock: current code/tests and the approved brief.
- Owner Constraints: billing serialization/debits stay in `billing.py`; the
  existing admin-only batch read and admin routes stay in `admin.py`; timing
  stays in `main.py`.
- Compatibility Boundary: existing fields and single-window endpoint remain.
- Retirement Boundary: keep per-user list calls retired, remove the duplicate
  active/recent traffic-pack queries and window dependency, retire triple UI
  calls, duplicate cold builds, and per-page historical scans from the main path.
- Test Obligations: RED/GREEN per task plus full admin regression.
- Drift Rule: any schema or billing-write requirement returns to design review.
- Evidence Required: query counts, cache call counts, passing tests, frontend
  build, and clean diff check.
- Advisory Boundary: method-pack execution guidance only; not authoritative
  completion.
