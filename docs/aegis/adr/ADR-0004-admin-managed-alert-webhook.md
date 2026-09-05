# ADR-0004 - Make SystemSetting the primary alert webhook owner

Status: `recorded-from-work`
Date: `2026-07-22`

## Source Evidence

- Implemented and reviewed runtime persistence, protected admin configuration, and explicit-empty behavior for fallback_alert_webhook_url.
## Context

Operators need to rotate, inspect, disable, and test the DingTalk destination from the existing Service Reliability control plane. Keeping the destination exclusively in Railway prevented complete administration and made routine rotation depend on deployment access.

## Decision

Store the validated DingTalk webhook URL in plaintext under the fallback_alert_webhook_url SystemSetting key. A present database key is authoritative, including an empty string that explicitly disables delivery. The Railway environment value is retained only as a compatibility fallback when the database key has never been created. Protected admin config reads and writes may return the complete value with no-store responses; runtime delivery resolves from the in-memory settings snapshot.

## Alternatives Considered

- Keep Railway as the sole owner; rejected because administrators could not view, rotate, or explicitly disable the destination from the application control plane.
- Encrypt the value at rest; rejected by explicit product choice in favor of the existing simple SystemSetting persistence contract.
- Create a second webhook table or settings page; rejected because it would duplicate ownership and navigation.
## Consequences

- Database administrators and authenticated application administrators can read the plaintext destination, so access to both boundaries must remain restricted.
- Saving an empty value is a durable disable action and does not revive the Railway value.
- Customer request handling adds no database or network wait; updates propagate through the existing runtime-settings refresh path.
## Compatibility Boundary

Existing alert policy fields, AlertEvent and RequestLog semantics, DingTalk delivery, Claude routing, billing, and the Service Reliability navigation owner remain unchanged. Railway configuration continues to bootstrap installations that have no database override.

## Retirement Impact

Railway-only webhook ownership is retired. The Railway variable itself is retained as a compatibility fallback and may be removed only through a separately planned migration after all installations have database state.

## Baseline Sync

- Needed: needed
- Target: docs/aegis/baseline/service-reliability.md
- Action: update baseline
- Reason: The canonical owner, admin read contract, compatibility fallback, and secret exposure boundary changed.

## Evidence References

- docs/aegis/work/2026-07-22-alert-webhook-management/evidence-bundle-draft-backend-runtime-red-green-review.json
## Supersedes

- ADR: docs/aegis/adr/ADR-0003-alert-delivery-audit-boundary.md
- Reason: ADR-0003 deliberately rejected database-backed webhook ownership; the implemented administrator-managed SystemSetting contract replaces that portion while retaining its request-failure and delivery-audit separation.
## Boundary

This ADR is an advisory Aegis Method Pack record. It does not grant completion authority or replace project-authoritative architecture sources.
