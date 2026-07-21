# Admin Alert Center Implementation Plan

## Goal

Add safe alert policy management, configuration testing, and durable DingTalk
delivery history to the existing Service Reliability admin surface.

## Architecture

- Keep `app/fallback_alerts.py` as trigger/delivery owner and add an immutable
  runtime-policy view whose database overrides fall back to environment values.
- Add `AlertEvent` to the existing SQLAlchemy model owner.
- Add `app/alert_history.py` for best-effort pending/completion persistence.
- Add `app/alert_admin.py` for protected config, test, and history APIs instead
  of growing the 6,000-line `app/admin.py`.
- Extend existing `SystemSetting` refresh and Service Reliability assets.

## Tech Stack

FastAPI, SQLAlchemy async sessions, MySQL, existing static admin HTML/CSS/JS,
pytest, and Railway environment variables.

## Baseline / Authority Refs

- `docs/aegis/specs/2026-07-21-admin-alert-center-brief.md`
- `docs/aegis/baseline/service-reliability.md`
- `docs/architecture/claude-code-upstream-runbook.md`
- `app/fallback_alerts.py`, `app/system_settings.py`, `app/reliability.py`
- `app/static/admin_assets/service-reliability.js`
- Baseline: 151 focused tests passed before edits.

## Compatibility Boundary

Do not change public APIs, channel selection, fallback order, billing, request
logging, Redis burst semantics, or the Railway webhook secret boundary. No new
database/network await may enter the customer request path.

## Verification

```bash
env COINCOIN_DATABASE_URL=mysql://test@127.0.0.1:3306/test \
  .venv/bin/python \
  -m pytest tests/test_fallback_alerts.py tests/test_alert_admin.py \
  tests/test_admin_usage_fields.py tests/test_reliability.py -q

.venv/bin/python \
  -m compileall -q app tests/test_alert_admin.py

git diff --check
```

## Aegis Visibility

Planning is required because this slice adds persistent audit state and a
runtime configuration contract while preserving a strict no-I/O request path.

## Requirement Ready Check

- Requirement source: user-approved conversation and the alert-center brief.
- Scenario: administrator configures and verifies DingTalk alerting.
- Acceptance: six criteria in the brief.
- Open blockers: none; webhook editing is explicitly excluded.
- Decision: ready.

## Change Necessity

- No-change option leaves successful push history invisible and thresholds
  Railway-only.
- Existing RequestLog cannot prove whether DingTalk delivery succeeded.
- Minimum boundary: one audit table, isolated admin/history modules, runtime
  settings refresh, and additions to the existing reliability UI.
- Decision: code-change.

## Existence Check

- Reuse `SystemSetting`, Service Reliability, and fallback delivery ownership.
- Add `AlertEvent` because no existing table represents notification delivery.
- Add small backend owner modules because `admin.py` is already over 6,000 lines.
- Do not add a new page, navigation owner, secret store, or duplicate failure log.
- Decision: add-with-proof for persistence/API modules; reuse-existing for UI.

## Architecture Integrity Lens

- RequestLog owns failures; AlertEvent owns outbound notification attempts.
- Railway owns the webhook secret; SystemSetting owns non-secret overrides.
- `fallback_alerts.py` owns trigger/delivery; the admin API only configures and
  reads it.
- Verdict: proceed.

## Performance and complexity budget

- Request path: no new await; only existing bounded task scheduling.
- Alert path: two best-effort DB writes only when a notification is actually sent.
- Admin reads: indexed, capped at 100 rows, active-page polling only.
- Avoid adding alert API/query code to `admin.py` or reliability aggregation.

## Task 1: Runtime policy and persistent event owner

Files: modify `app/config.py`, `app/models.py`, `app/system_settings.py`,
`app/fallback_alerts.py`; create `app/alert_history.py`; modify
`tests/test_fallback_alerts.py`.

Why: make non-secret policy runtime-editable and preserve delivery evidence
without adding request-path I/O.

Impact/compatibility: environment defaults remain valid; DB overrides are
optional; webhook remains environment-only.

- [x] Write tests for policy precedence, disabled scheduling, pending/sent/failed
  event persistence hooks, and absence of synchronous DB work on scheduling.
- [x] Run focused tests and verify RED.
- [x] Add the policy snapshot, supported setting keys, AlertEvent model/indexes,
  and best-effort event lifecycle calls around outbound sends.
- [x] Run focused tests and verify GREEN.
- [x] Commit the runtime/persistence slice.

## Task 2: Protected alert admin API

Files: create `app/alert_admin.py`, create `tests/test_alert_admin.py`, modify
`app/main.py`.

Why: expose configuration state, safe updates, one labelled test send, and
bounded history without expanding the monolithic admin router.

Impact/compatibility: all endpoints use the existing admin guard; responses
contain no webhook value.

- [x] Write API tests for masking, validation, persistence/apply, history
  filters, and successful/failed configuration tests.
- [x] Run tests and verify RED.
- [x] Implement `GET/PATCH /admin/alerts/config`, `POST /admin/alerts/test`, and
  `GET /admin/alerts/events` with a 100-row cap.
- [x] Run tests and verify GREEN.
- [x] Commit the protected API slice.

## Task 3: Service Reliability alert controls and history

Files: modify `app/static/admin.html`,
`app/static/admin_assets/service-reliability.js`,
`app/static/admin_assets/service-reliability.css`, and
`tests/test_admin_usage_fields.py`.

Why: keep alert operations beside channel/model reliability instead of adding
another navigation surface.

Impact/compatibility: existing overview polling remains; alert config/history
loads only while the page is active.

- [x] Add static contract tests for controls, masked webhook state, test action,
  history table, and absence of a new navigation page.
- [x] Run tests and verify RED.
- [x] Add the policy form, status summary, test button, filters, history renderer,
  and responsive styles to existing reliability assets.
- [x] Run tests and verify GREEN.
- [x] Commit the admin UI slice.

## Task 4: Documentation, review, and ship

Files: modify `docs/architecture/claude-code-upstream-runbook.md`,
`docs/aegis/baseline/service-reliability.md`, and `docs/aegis/INDEX.md`.

Why: keep runtime ownership, secret boundary, and operator behavior discoverable.

Impact/compatibility: documentation only after implementation behavior is final.

- [x] Update the runbook and baseline with the final owner/performance contract.
- [x] Run focused backend tests, frontend/static tests, compile checks, and
  `git diff --check`.
- [x] Run an independent pre-landing review and fix concrete findings.
- [ ] Push the feature branch, create a PR, merge to `master`, and remove the
  remote feature branch.
- [ ] Confirm Railway deployment success and `GET https://coincoin.ai/health`.

## Execution Readiness View

- Intent lock: manage and audit DingTalk alerts in existing reliability UI.
- Scope fence: non-secret policy, test send, delivery history; no webhook editing.
- Baseline lock: RequestLog, SystemSetting, fallback delivery, reliability page.
- Compatibility: no public API, routing, billing, or request-path I/O change.
- Test obligations: policy, persistence, API auth/validation, UI contract,
  fallback regression, deployment health.
- Drift rule: stop if implementation requires exposing the webhook or awaiting
  DB/network work from the customer request coroutine.
- Evidence: focused/full differential tests, review, merged PR, Railway success.

## Risks and rollback

- DB outages may leave an alert event missing; delivery must still proceed.
- Multiple replicas converge through the existing settings refresh interval.
- Rollback removes the router/UI and stops new rows; existing audit rows remain
  harmless and environment-based alerting continues.

## Retirement

No runtime path is retired. The plan deliberately rejects a second admin page
and a database-backed webhook secret owner.
