# Alert Webhook Management Implementation Plan

## Goal

Implement the approved database-first DingTalk Webhook management contract in the
existing alert admin API and Service Reliability page, then initialize and test it
in production without committing the real URL.

## Architecture

- Reuse `SystemSetting`, `alert_admin.py`, `fallback_alerts.py`, the runtime settings
  refresh loop, and the existing Service Reliability alert form.
- Store plaintext under `fallback_alert_webhook_url` only after administrator save.
- Resolve Webhook once into in-memory runtime settings; database row presence wins,
  including an explicit empty value, while row absence falls back to environment.
- Supersede ADR-0003's environment-only secret-owner decision after verification.

## Tech Stack

FastAPI, Pydantic, SQLAlchemy/MySQL, existing static HTML/CSS/JavaScript, pytest,
GitHub, and Railway.

## Baseline / Authority Refs

- `docs/aegis/specs/2026-07-22-alert-webhook-management-brief.md`
- `docs/aegis/adr/ADR-0003-alert-delivery-audit-boundary.md`
- `docs/aegis/baseline/service-reliability.md`
- `docs/architecture/claude-code-upstream-runbook.md`
- `app/alert_admin.py`, `app/fallback_alerts.py`, `app/system_settings.py`
- `tests/test_alert_admin.py`, `tests/test_fallback_alerts.py`

## Compatibility Boundary

No public API, routing, billing, RequestLog, failure counter, or customer-path I/O
change. Environment configuration remains an active compatibility fallback only
when the database key is absent.

## Verification

```bash
env COINCOIN_DATABASE_URL=mysql://test@127.0.0.1:3306/test \
  .venv/bin/python -m pytest \
  tests/test_alert_admin.py tests/test_fallback_alerts.py \
  tests/test_admin_usage_fields.py tests/test_reliability.py -q
node --check app/static/admin_assets/service-reliability.js
.venv/bin/python -m compileall -q app tests
git diff --check origin/master
```

## Aegis Visibility

Planning is required because the change deliberately moves a source-of-truth and
exposes a credential value through an administrator contract while preserving a
non-blocking request path.

## Requirement Ready Check

- Requirement source: explicit user approval in the current conversation.
- Scenario: an authenticated administrator manages DingTalk delivery from Service Reliability.
- Acceptance: eight items in the approved brief.
- Open blockers: none.
- Decision: ready.

## Change Necessity

- A Railway-only configuration cannot be read or changed from the admin page.
- The minimum code boundary is the existing alert API, runtime setting resolver,
  sender lookup, existing UI form, tests, and current documentation owners.
- Decision: code-change.

## Existence Check

- No new runtime owner, page, endpoint, table, or secret service is needed.
- Reuse `SystemSetting`, the alert admin router, and Service Reliability.
- Decision: reuse-existing.

## Architecture Integrity Lens

- Canonical owner: database row when present; environment fallback when absent.
- Runtime contract: an in-memory resolved value, never a per-request database read.
- Responsibility overlap: avoided by explicit presence-based precedence.
- Verdict: proceed.

## Anti-Entropy Declaration

- Deletion class: contract-carrying configuration behavior.
- Old path: environment-only Webhook ownership.
- New canonical owner: `SystemSetting` after the first admin save.
- Preserved behavior: existing deployments continue sending before the first save.
- Retired behavior: admin responses no longer mask the Webhook.
- External boundary touched: yes, Railway startup configuration.
- Source-of-truth data risk: possible but non-destructive; no existing row is deleted.
- User confirmation required: no; the user explicitly selected plaintext storage.

## Retirement Decision

- Path: compat-exception.
- Why: the current production deployment actively depends on the Railway value.
- Retirement trigger: after every deployment environment has a verified database
  override and operators approve removal of the startup fallback.
- Non-edits: do not delete or rewrite Railway variables in this slice.

## Task 1: Backend contract and runtime owner

Files: modify `tests/test_alert_admin.py`, `tests/test_fallback_alerts.py`,
`app/alert_admin.py`, `app/system_settings.py`, and `app/fallback_alerts.py`.

Why: persist, return, validate, and use the administrator-selected URL without
adding request-path I/O.

Impact/compatibility: row presence must shadow the environment even when empty;
existing installations without a row keep current behavior.

- [x] Write failing tests for full GET, no-store headers, plaintext UPSERT,
  validation, empty override, environment fallback, and sender resolution.
- [x] Run the focused tests and verify RED.
- [x] Add the supported setting key, presence-aware resolver, Pydantic validation,
  atomic UPSERT, and immediate runtime apply at existing canonical owners.
- [x] Run focused tests and verify GREEN.
- [x] Commit backend behavior with its tests.

## Task 2: Existing administrator page

Files: modify `app/static/admin.html`,
`app/static/admin_assets/service-reliability.js`,
`app/static/admin_assets/service-reliability.css`, and
`tests/test_admin_usage_fields.py`.

Why: let the administrator view and change the complete value without another page.

Impact/compatibility: polling remains active-page-only; saving one form updates the
Webhook and policy atomically.

- [x] Write failing static contract tests for the URL field, payload, complete
  rendering, validation messaging, and removal of environment-only wording.
- [x] Run the static contract test and verify RED.
- [x] Add the URL field and wire it into render, dirty tracking, validation, and save.
- [x] Run static contract and JavaScript syntax checks and verify GREEN.
- [x] Commit the UI behavior with its test.

## Task 3: Architecture records and release verification

Files: create `docs/aegis/adr/ADR-0004-admin-managed-alert-webhook.md`; modify
`docs/aegis/baseline/service-reliability.md`,
`docs/architecture/claude-code-upstream-runbook.md`, and `docs/aegis/INDEX.md`.

Why: keep the deliberate plaintext/source-of-truth reversal discoverable.

Impact/compatibility: documentation must never include the real URL or token.

- [x] Update the ADR, baseline, runbook, and Aegis index with the implemented contract.
- [ ] Run focused tests, compile/static checks, docs checks, and diff checks.
- [ ] Run pre-landing review and fix any concrete issues.
- [ ] Push, create a PR, merge to `master`, and verify Railway deployments/health.
- [ ] Write the production URL through the protected API, send one labelled test,
  and verify the database-backed value plus successful AlertEvent history.

## Plan Pressure Test

- Owner/contract: explicit and reused.
- Verification: backend, UI, docs, deployment, and live initialization covered.
- Task executability: exact files and commands identified.
- Pressure result: proceed.

## Plan-Time Complexity Check

- `alert_admin.py` and `system_settings.py` remain small owner files.
- `fallback_alerts.py` changes only URL resolution call sites.
- The large admin HTML is edited only in its existing Service Reliability section.
- Recommendation: edit-in-place; no new runtime module.

## Execution Readiness View

- Intent lock: admin fully controls the DingTalk Webhook.
- Scope fence: one URL plus existing alert policy; no keyword or routing changes.
- Baseline lock: current alert API, SystemSetting, runtime refresh, and sender owners.
- Compatibility: environment fallback only when the database row is absent.
- Retirement: environment-only ownership ends; startup fallback remains bounded.
- Test obligations: auth, validation, precedence, empty override, UI, no-store, sender.
- Review gates: secret scan, focused/full differential tests, pre-landing review.
- Drift rule: stop if the real URL appears in Git/logs or a database await enters request handling.

## Risks and rollback

- Plaintext database exposure is an explicit accepted risk; admin and DB access remain
  the security boundary.
- Rollback removes the database key from supported runtime settings and returns to
  the existing Railway value. It does not delete live rows automatically.
- A malformed stored legacy value must be visible to the admin but must not be sent
  until a valid save replaces it.
