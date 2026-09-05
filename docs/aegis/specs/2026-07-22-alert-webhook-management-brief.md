# Alert Webhook Management Brief

## Goal

Allow an authenticated administrator to view, replace, clear, save, and test the
complete DingTalk robot Webhook from the existing Service Reliability page.

## Approved behavior

- The administrator page displays the complete effective Webhook value and edits
  it in the same form as the alert thresholds.
- `coincoin_system_settings.setting_key = fallback_alert_webhook_url` stores the
  administrator-saved Webhook as plaintext. This is an explicit user decision.
- A database row is the canonical runtime value. The Railway environment value is
  used only when that row does not exist, preserving currently deployed instances.
- Saving an empty value creates an explicit empty override and disables delivery;
  it must not silently fall back to the environment value.
- The existing protected `GET/PATCH /admin/alerts/config` contract carries the
  full `webhook_url`; no new page or endpoint owner is added.
- Config responses set `Cache-Control: no-store`. Application logs, AlertEvent,
  RequestLog, tests, docs, commits, and PR bodies never contain the production URL.
- Only an HTTPS DingTalk robot URL on `oapi.dingtalk.com` with `/robot/send` and a
  non-empty `access_token` query parameter is accepted.
- Saving updates the current replica immediately and other replicas through the
  existing runtime settings refresh loop.
- After deployment, the previously supplied production Webhook is written through
  the authenticated admin API and one labelled configuration test is sent.

## Compatibility boundary

- Public request APIs, Claude routing, fallback order, billing, RequestLog, and
  alert thresholds remain unchanged.
- Successful customer requests perform no new database or network work.
- Alert delivery reads an in-memory resolved Webhook; it does not query the
  database from the customer request path or background sender.
- Existing Railway-only installations continue working until an administrator
  saves a database override.

## Acceptance

1. An unauthenticated caller cannot read or change the Webhook.
2. An authenticated admin receives the complete effective URL with no-store
   response headers.
3. A valid save persists the exact plaintext URL and applies it immediately.
4. Invalid, non-HTTPS, non-DingTalk, or tokenless URLs are rejected before commit.
5. An explicit empty save shadows the environment value and disables test/send.
6. Replica refresh loads the database override without exposing it in logs.
7. The existing admin page can edit the value and send a configuration test.
8. The production URL is absent from the Git diff and appears only in the live
   database after the deployment mutation.

## Non-goals

- Encrypting the database value.
- Editing the DingTalk keyword.
- Adding a second alert page or secret-management service.
- Changing failure counters, fallback routing, or alert-history semantics.

## ADR signal

This reverses the Webhook ownership portion of ADR-0003. A superseding ADR must
record the database-first owner, explicit plaintext decision, environment fallback,
admin exposure, and rollback boundary after implementation is verified.
