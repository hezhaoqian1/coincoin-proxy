# Channel Representative Probe Implementation Plan

Goal: Replace route-derived multi-model probing with one representative model per provider channel, automatic highest-priority selection with administrator override, and channel-first reliability semantics.

Architecture: Reuse `ProviderChannelMonitor` as the single probe owner; reconcile one active monitor per channel; separate channel probe health from public-model routing/traffic health; keep `app/channel_router.py` untouched.

Tech Stack: FastAPI, SQLAlchemy async, MySQL, httpx, vanilla JavaScript/CSS, unittest/pytest.

Baseline/Authority Refs: `docs/aegis/specs/2026-07-15-channel-representative-probe-design.md`, `docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md`, `docs/aegis/baseline/service-reliability.md`.

Compatibility Boundary: Preserve provider channel/route CRUD, discovery, connection testing, routing, priority, weight, cooldown, fallback, streaming, billing, manual monitor APIs, and retained history. No reliability I/O enters customer request paths.

Verification: Focused unit/API tests, the existing 245-test compatibility suite, JavaScript/Python syntax checks, credential scan, desktop/mobile browser QA, and one production explicit probe.

## Requirement Ready Check

- Requirement source: user-approved conversation and the design spec above.
- Scenario: an operator needs to know whether a channel can serve one representative real model without treating that result as every model's health.
- Acceptance: deterministic auto selection, manual override, one request per probe, no `/models`, no model-state propagation, channel-first UI, no routing side effects.
- Open blockers: none.
- Decision: `ready`.

## Change Necessity

- Current reconciliation executes up to three models per endpoint and the overview propagates channel probe failure into public-model status.
- Configuration alone cannot change those runtime semantics or provide an administrator override.
- Minimum boundary: `app/channel_monitoring.py`, `app/reliability.py`, admin monitor-selection API/payloads, channel modal, reliability assets, focused tests, and architecture docs.
- Decision: `code-change`.

## Architecture Integrity

- Canonical probe owner: `ProviderChannelMonitor` and `app/channel_monitoring.py`.
- Canonical routing owner: unchanged `app/channel_router.py`.
- Retire: active use of `extra_models`, per-endpoint automatic monitor ownership, model health inherited from channel probe state, and `/models` preflight health judgment.
- Preserve: history rows, compatibility APIs, leases, and explicit probe action.

## Task 1: Representative selection and single-probe execution

Files: `app/channel_monitoring.py`, `tests/test_channel_monitoring.py`.

Why: Reduce probe cost and align the monitored object with the operator-controlled channel.

Impact/Compatibility: No request routing code changes. Existing history remains; redundant automatic monitors are disabled.

Verification: `pytest -q tests/test_channel_monitoring.py`.

- [ ] Write tests for priority/weight/id selection, one monitor per channel, manual override preservation, invalid override disablement, legacy monitor collapse, and exactly one POST with no GET `/models`.
- [ ] Run the focused tests and confirm the new assertions fail against the current implementation.
- [ ] Implement channel-level selection, reconciliation, single-model execution, minimal prompt, and structural response validation.
- [ ] Run the focused tests and confirm they pass.
- [ ] Commit as `refactor: probe one model per provider channel`.

## Task 2: Channel monitor-selection API and payload

Files: `app/schemas.py`, `app/admin.py`, `tests/test_admin_usage_fields.py`, `tests/test_reliability.py`.

Why: Let operators inspect, override, and reset the representative route model from the channel workflow.

Impact/Compatibility: Add an admin-only selection endpoint and additive channel payload fields; retain existing monitor endpoints.

Verification: `pytest -q tests/test_admin_usage_fields.py tests/test_reliability.py`.

- [ ] Write tests for channel payload monitor metadata, valid manual selection, invalid/non-active route rejection, reset-to-auto, and cache invalidation.
- [ ] Run focused tests and confirm RED.
- [ ] Add the selection schema, validation/upsert helper, endpoint, and channel payload join.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit as `feat: configure channel probe models`.

## Task 3: Channel-first reliability semantics and UI

Files: `app/reliability.py`, `app/static/admin.html`, `app/static/admin_assets/service-reliability.js`, `app/static/admin_assets/service-reliability.css`, `tests/test_reliability.py`, `tests/test_admin_usage_fields.py`.

Why: Prevent probe failures from being misread as every model failing and make the dashboard answer the channel-operation question first.

Impact/Compatibility: Overview response remains additive-compatible; channel/model arrays remain, but model health stops consuming probe state and channels render first.

Verification: focused reliability/admin tests, `node --check`, desktop/mobile browser QA.

- [ ] Write tests proving a failed channel probe does not fail public-model health and that channel summaries/incidents are primary.
- [ ] Run focused tests and confirm RED.
- [ ] Separate routing health from probe health, update summary fields, reorder/relabel the UI, and add the channel edit monitor selector with auto/manual modes.
- [ ] Run focused tests and JavaScript syntax check; verify GREEN.
- [ ] Commit as `feat: make reliability monitoring channel first`.

## Task 4: Migration compatibility and architecture sync

Files: `docs/aegis/adr/ADR-0002-route-derived-reliability-observation.md`, `docs/aegis/baseline/service-reliability.md`, focused tests as required.

Why: Retire the wrong multi-model ownership rule without deleting persistent history or breaking manual API callers.

Impact/Compatibility: No destructive database migration. Legacy automatic monitors are disabled by reconciliation; `extra_models` remains a compatibility field but is not executed.

Verification: full compatibility suite, Aegis workspace check, diff/credential scans.

- [ ] Add migration/compatibility assertions for retained history and disabled redundant monitors.
- [ ] Run focused tests and confirm expected coverage.
- [ ] Amend ADR-0002 and the reliability baseline to the channel representative-probe decision.
- [ ] Run the full suite and static checks.
- [ ] Commit as `docs: align reliability channel probe architecture`.

## Task 5: Review, ship, and production acceptance

Files: branch diff only; no planned source edits unless review or QA finds a verified defect.

Why: The change alters monitoring semantics and production admin workflows.

Impact/Compatibility: Merge only after no unresolved P1/P2 and successful browser/production evidence.

Verification: final fresh tests, GitHub PR checks, Railway deployment status, production admin/API/browser checks.

- [ ] Run pre-landing review focused on routing isolation, monitor reconciliation, schema/session safety, and UI side effects.
- [ ] Fix verified findings with focused regression tests and rerun the affected suite.
- [ ] Run final tests, syntax checks, diff check, and public-repository credential scan.
- [ ] Push, create and merge the PR to `master`.
- [ ] Confirm Railway deploy success, verify automatic selection and manual override in the admin UI, execute one healthy-channel probe, and confirm routing configuration did not change.

## Risks and Rollback

- Invalid legacy manual monitors may no longer run; they remain visible and retained for operator correction.
- Automatic choice can change when route priority changes; the UI must display the selected route/model explicitly.
- A model request can consume a tiny amount of upstream quota; one request per interval is the bounded cost.
- Rollback is the merge revert; no destructive schema or data deletion is required.
