# ADR-0003 - Separate request failures from alert delivery audit

Status: `recorded-from-work`
Date: `2026-07-21`

## Source Evidence

- Implemented runtime policy, AlertEvent persistence, protected admin API, and existing-page UI with 173 focused regression tests passing.
## Context

Operators need to know whether DingTalk received an alert without duplicating every upstream failure or moving the webhook secret into application-managed persistence.

## Decision

RequestLog remains the source of truth for every upstream failure. AlertEvent records only actual DingTalk delivery attempts and sanitized outcomes. Railway environment configuration remains the sole webhook-secret owner, while SystemSetting stores only validated non-secret policy overrides. All persistence and delivery awaits run in bounded background alert tasks, never in the customer request coroutine.

## Alternatives Considered

- Store every failure again in alert history; rejected because it duplicates RequestLog and adds write amplification during incidents.
- Store and edit the webhook in the database/admin UI; rejected because it creates a second secret owner and increases exposure risk.
- Keep all policy and delivery evidence only in Railway/log output; rejected because operators cannot safely inspect or change non-secret policy from the existing reliability control plane.
## Consequences

- Operators can inspect sent, failed, pending, and configuration-test attempts without accessing Railway logs or webhook contents.
- Dedup-suppressed failures create no AlertEvent rows and remain observable through RequestLog.
- A database audit failure cannot suppress DingTalk delivery; a delivery failure cannot fail a customer request.
## Compatibility Boundary

Public APIs, Claude routing, fallback order, billing, RequestLog semantics, and webhook ownership remain unchanged. The existing Service Reliability page is extended without adding a navigation owner.

## Retirement Impact

No runtime path is retired. The design explicitly rejects duplicate failure persistence, a second alert page, and a database-backed webhook owner; rollback may leave inert AlertEvent rows.

## Baseline Sync

- Needed: needed
- Target: docs/aegis/baseline/service-reliability.md
- Action: update baseline
- Reason: The ownership map, request-path performance boundary, admin read flow, and secret boundary now include alert delivery audit.

## Evidence References

- docs/aegis/work/2026-07-21-admin-alert-center/evidence-bundle-draft-runtime-policy-alert-history-red-green.json
- docs/aegis/work/2026-07-21-admin-alert-center/evidence-bundle-draft-alert-admin-api-red-green.json
- docs/aegis/work/2026-07-21-admin-alert-center/evidence-bundle-draft-alert-ui-focused-regression.json
## Boundary

This ADR is an advisory Aegis Method Pack record. It does not grant completion authority or replace project-authoritative architecture sources.

## Superseded By

- Status: superseded
- Date: 2026-07-22
- ADR: docs/aegis/adr/ADR-0004-admin-managed-alert-webhook.md
- Reason: ADR-0003 deliberately rejected database-backed webhook ownership; the implemented administrator-managed SystemSetting contract replaces that portion while retaining its request-failure and delivery-audit separation.
