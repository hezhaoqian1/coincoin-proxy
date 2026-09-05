# Service Reliability Center

## Goal

Replace the three overlapping admin monitoring surfaces with one cached service-reliability console that automatically reflects provider channels and model routes, while preserving every existing provider-channel create, update, connection-test, model-discovery, route, fallback, priority, weight, and cooldown contract.

## Architecture

- Add `app/reliability.py` as the read-model owner for the reliability console.
- Build the overview from existing `ProviderChannel`, `ModelChannelRoute`, `ProviderChannelRuntimeState`, `ProviderChannelMonitor`, monitor history/rollups, and recent `RequestLog` rows.
- Cache the completed overview in process for 10 seconds. Dashboard polling reads this cache and never triggers a model request.
- Add route-derived monitor reconciliation to `app/channel_monitoring.py`. Active routes remain the source of truth; existing manual monitors are reused as compatibility carriers when they already cover a channel and endpoint.
- Keep `/ops/monitoring/probes/*` as the external end-to-end monitoring boundary. Keep explicit Run now actions for operators.
- Replace the old admin pages with a single `service-reliability` page. Keep provider channel configuration separate and lightweight.

## Tech Stack

- FastAPI and SQLAlchemy async sessions
- Existing MySQL models and indexes
- Existing static admin HTML/CSS/JavaScript
- Pytest/Unittest-compatible test suite

## Baseline / Authority Refs

- User-approved design discussion in this task: one reliability owner, route-derived monitors, no user-facing upstream disclosure, no hot-path performance regression.
- `app/channel_monitoring.py`: current active-probe owner.
- `app/monitoring.py`: protected external end-to-end probes.
- `app/channel_router.py`: current in-memory fallback/cooldown behavior, unchanged in this slice.
- `app/admin.py`: provider-channel and model-route contracts that must remain stable.
- Baseline verification: 94 focused tests passed on `origin/master` before edits.

## Compatibility Boundary

- Do not change request routing, fallback selection, billing, prompt transformation, provider authentication, or stream handling.
- Do not add database, Redis, webhook, or network awaits to the user request path.
- Do not remove `/health`, `/ops/monitoring/*`, provider-channel CRUD, connection testing, upstream-model discovery, model-route CRUD, or cooldown clearing.
- Do not drop or delete persistent monitor/history tables or production rows.
- Existing manual monitor API endpoints remain temporarily available even after their duplicate admin UI is retired.

## Verification

```bash
COINCOIN_DATABASE_URL="${COINCOIN_TEST_DATABASE_URL:?set COINCOIN_TEST_DATABASE_URL}" \
  .venv/bin/python -m pytest -q \
  tests/test_reliability.py \
  tests/test_channel_monitoring.py \
  tests/test_monitoring_probes.py \
  tests/test_admin_usage_fields.py \
  tests/test_openai_compat_defaults.py \
  tests/test_anthropic_compat.py

.venv/bin/python -m py_compile \
  app/reliability.py app/channel_monitoring.py app/main.py
```

Browser verification must cover 1440x900 and 390x844, confirm no console errors, and verify that loading or refreshing the reliability page does not call any `/probes/*` endpoint.

## Aegis Visibility

Planning is required because this work creates a new canonical read-model owner, retires duplicate internal UI owners, and changes how monitor configuration is derived without deleting persistent state.

## BaselineUsageDraft

- Required baseline refs: current channel, monitor, ops probe, router, and admin UI owners listed above.
- Delivered context refs: user-approved monitoring and page behavior in this task.
- Acknowledged before plan refs: focused test baseline and current source files.
- Cited in plan refs: all required baseline refs.
- Missing refs: none.
- Decision: continue.

## Requirement Ready Check

- Requirement source refs: user-approved conversation.
- Goals and scope refs: one reliability page, automatic visibility after channel/route creation, performance isolation, duplicate UI retirement.
- User / scenario refs: CoinCoin administrator diagnosing provider channel failures and fallback behavior.
- Requirement item refs: cached overview, route-derived monitoring, quiet operational UI, preserved channel management.
- Acceptance / verification criteria refs: compatibility and verification sections above.
- Open blocker questions: none for phase one; automatic route-state enforcement remains intentionally out of scope.
- Decision: ready.

## Change Necessity

- User-visible need: channel additions and model routes must appear automatically in one useful status page.
- No-change / non-code option: Railway logs and current pages require manual correlation and duplicate monitor setup.
- Why code change is necessary: existing owners cannot provide a cached route-centric overview or automatic monitor derivation.
- Minimum change boundary: new read-model router, monitor reconciler, admin page replacement, and focused tests.
- Decision: code-change.

## Existence Check

- Proposed new surface: `app/reliability.py` and dedicated reliability page assets.
- Existing owner / reuse candidate: `app/admin.py`, `app/monitoring.py`, and `app/channel_monitoring.py`.
- Why existing surface is insufficient: `admin.py` and `admin.html` already exceed maintainable size; `monitoring.py` executes probes and is not a cached operational read model.
- Creation proof: the new files compose existing owners without duplicating routing or probing behavior.
- Entropy / retirement impact: removes two navigation pages and the embedded manual-monitor surface.
- Decision: add-with-proof.

## Architecture Integrity Lens

- Invariant: routes configure delivery; reliability observes delivery; router owns request selection.
- Canonical owner / contract: model routes derive monitor targets; `app/reliability.py` owns the admin read model.
- Responsibility overlap: old monitoring and ops-health pages are retired after their data is composed into the new page.
- Higher-level simplification: dashboard reads stored state and aggregates instead of invoking probes.
- Retirement / falsifier: if any removed UI still carries a unique operation, migrate that operation before deletion.
- Verdict: proceed.

## Complexity Budget

- Artifact class: backend read model plus admin operational UI.
- Target files / artifacts: new `app/reliability.py`, new static reliability assets, small edits to `main.py`, `channel_monitoring.py`, and `admin.html`.
- Current pressure: `app/admin.py` is over 5,000 lines and `admin.html` is over 7,000 lines.
- Projected post-change pressure: within budget only if new logic is extracted instead of added inline.
- Budget result: at-risk.
- Planned governance: new backend and frontend owner files; delete obsolete inline monitoring UI/JS.

## Execution Readiness View

- Intent Lock: consolidate observation without changing request routing.
- Scope Fence: admin reliability page, cached read model, automatic monitor reconciliation, duplicate UI retirement.
- Baseline Lock: 94 focused tests pass before edits.
- Approved Behavior: new channels appear immediately; routes derive monitoring; page refresh is read-only.
- Owner / Contract Constraints: provider channel and model route APIs remain stable.
- Compatibility Boundary: protected external probes and manual monitor APIs remain available.
- Retirement Boundary: UI and internal duplicate owners only; no persistent-state deletion.
- Task Batches: backend read model, monitor reconciliation, frontend consolidation, verification.
- Test Obligations: endpoint payloads, caching, reconciliation, UI wiring, existing channel/fallback suites.
- Review Gates: focused tests, browser QA, diff review, performance-path inspection.
- Drift / Rewind Rules: stop if implementation requires request-path I/O or destructive schema changes.
- Evidence Required Before Completion: passing tests, browser screenshots, zero probe calls on dashboard load, clean diff review.
- Advisory Boundary: method-pack execution guidance only; not completion authority.

## Tasks

### Task 1: Cached reliability read model

Files: create `app/reliability.py`, create `tests/test_reliability.py`, modify `app/main.py`.

Why: provide a single inexpensive API for the new console without changing request processing.

Impact / Compatibility: read-only database queries; 10-second in-process cache; no probe execution.

- [x] Write tests proving all channels appear, route-less channels show `unconfigured`, routed but unchecked channels show `pending`, monitor failures and fallback traffic produce degraded/failed summaries, and repeated calls hit cache.
- [x] Run the tests and verify they fail because the endpoint does not exist.
- [x] Implement `/admin/reliability/overview` and cache reset helper.
- [x] Run the focused tests and verify they pass.
- [x] Commit the backend read-model slice.

### Task 2: Route-derived monitor reconciliation

Files: modify `app/channel_monitoring.py`, modify route mutation hooks in `app/admin.py`, modify `tests/test_channel_monitoring.py` and `tests/test_admin_usage_fields.py`.

Why: adding a route should make monitoring automatic instead of requiring a second manual configuration.

Impact / Compatibility: reuse a covering active legacy monitor; otherwise create one deterministic auto monitor per channel and endpoint, capped to three distinct route models. Disable auto monitors when their routes disappear. Do not delete manual rows.

- [x] Write reconciliation tests for create, reuse, update, disable, and the three-model cap.
- [x] Verify RED.
- [x] Implement deterministic auto-monitor IDs and reconciliation.
- [x] Invoke reconciliation after route mutations and periodically in the monitor loop.
- [x] Verify GREEN and commit.

### Task 3: Service reliability admin page

Files: create `app/static/admin-reliability.css`, create `app/static/admin-reliability.js`, modify `app/static/admin.html`, modify `tests/test_admin_usage_fields.py`.

Why: administrators need one quiet operational console organized by public model and channel status.

Impact / Compatibility: retain provider-channel CRUD and route configuration; page polling reads only `/admin/reliability/overview` every 15 seconds and pauses while hidden.

- [x] Write static wiring tests for the new navigation/page and absence of old duplicate pages/manual monitor creation UI.
- [x] Verify RED.
- [x] Add the summary strip, incident band, model table, channel table, route detail drawer, and explicit Run now action.
- [x] Remove old realtime-monitoring, ops-health, and embedded manual-monitor markup/JavaScript while retaining shared helpers.
- [x] Verify GREEN and commit.

### Task 4: Compatibility and performance verification

Files: tests only unless a defect is found.

Why: prove the consolidation does not affect channel creation or user request performance.

Impact / Compatibility: no production mutation.

- [x] Run provider-channel CRUD, connection, model-discovery, route, fallback, Anthropic, and OpenAI compatibility tests.
- [x] Inspect the diff to prove no reliability code is imported by the request hot path.
- [x] Verify dashboard JavaScript never calls `/ops/monitoring/probes/*` or `/admin/monitoring/snapshot` during load/refresh.
- [x] Run browser QA at desktop and mobile sizes and inspect console/network errors.
- [x] Record verification evidence and commit any test-only corrections.

## Risks

- Existing manual monitors may overlap route-derived coverage. Reconciliation must reuse coverage and never delete production rows.
- Request-log aggregation can become expensive. Keep windows bounded, use indexed timestamps/channel IDs, cap detail rows, and cache the assembled response.
- Static admin monolith may retain dead JavaScript references. Lingering-reference checks and browser QA are mandatory.
- Anthropic/Claude Code routes require their existing specialized probe headers; reconciliation must only derive configuration and must not replace probe execution.

## Retirement

Anti-Entropy Declaration:
- Deletion Class: internal code/UI retirement.
- Old Path/Object: realtime-monitoring page, ops-health page, embedded manual monitor manager.
- New Canonical Owner: service reliability page and `app/reliability.py`.
- Expected Preserved Behavior: all channel management, protected probes, history, Run now, fallback, and cooldown behavior.
- Expected Retired Behavior: page-load probes and duplicate manual monitor setup.
- External Boundary Touched: protected probe endpoints remain stable.
- Source-of-Truth Data Risk: none in this plan; persistent rows are retained.
- User Confirmation Required: no for code/UI retirement; yes for any later table/row deletion.

Retirement Decision:
- Path: delete-first for duplicate internal UI after new owner passes tests.
- Why: no external consumer depends on admin HTML layout.
- Non-edits: no database drops, row deletion, request-router replacement, or public API removal.

Verification Plan:
- Main-path check: new page shows channel and route status and can trigger explicit probes.
- Lingering-reference check: old page IDs, loaders, and manual monitor controls are absent.
- Negative check: loading the new page does not execute probes.
- Boundary check: existing channel and external monitoring API tests remain green.
