# Admin Alert Center Brief

## Status

- Date: 2026-07-21
- Status: approved by user in conversation
- Change kind: admin control-plane extension and alert-delivery audit persistence

## Goal

Extend the existing Service Reliability page with alert configuration status,
safe runtime policy controls, a DingTalk configuration-test action, and durable
delivery history. Keep the DingTalk webhook exclusively in Railway environment
variables and never return or persist its value.

## User scenario

An administrator needs to answer four questions without reading Railway logs:

1. Is alerting enabled and is a DingTalk destination configured?
2. What thresholds and deduplication policy are active?
3. Did a particular alert reach DingTalk, fail, or remain pending?
4. Can the configured destination receive a clearly labelled test message?

## Baseline and authority

- `app/fallback_alerts.py` owns burst detection and DingTalk delivery.
- `RequestLog` owns per-upstream-attempt failure evidence.
- `SystemSetting` owns small runtime control-plane overrides.
- `app/reliability.py` and the Service Reliability page own operational reads.
- `docs/aegis/baseline/service-reliability.md` requires no database, Redis,
  webhook, or network await on the customer request path.
- Railway environment variables remain the secret source of truth.

## Approved behavior

### Existing request evidence

Do not duplicate request failures into alert history. `RequestLog` remains the
canonical per-attempt record. Alert history stores only actual delivery
attempts, including configuration tests.

### Runtime policy

The admin may edit these non-secret values:

- enabled
- availability/rate-limit threshold
- authentication threshold
- rolling window seconds
- deduplication seconds
- maximum pending alert tasks

Database values override environment defaults. Removing an override is not in
scope for this slice; the admin always writes a complete validated policy.
The existing runtime-system-settings refresh loop propagates changes to every
replica.

### Webhook boundary

- The full webhook remains only in `COINCOIN_FALLBACK_ALERT_WEBHOOK_URL`.
- Admin responses expose only `webhook_configured: true|false`.
- No endpoint returns, copies, encrypts, or persists the webhook.
- A test action sends one message containing the configured DingTalk keyword
  and the exact phrase `配置测试`; it is also recorded as an alert event.

### Alert events

Add an append/update audit row with a stable ID before outbound delivery. The
row contains category, severity, source fields, count/window, latest request ID,
destination type, delivery status, response status, a short sanitized error,
and timestamps. It never contains the webhook, raw upstream response, API key,
or raw DingTalk response body.

Delivery states are `pending`, `sent`, and `failed`. If persistence fails,
delivery still proceeds and the failure is logged; alert audit persistence must
never fail a customer request or suppress DingTalk delivery.

### Admin surface

Reuse the existing Service Reliability page. Add:

- a status/policy section with masked configuration state;
- save and configuration-test controls;
- a bounded recent-delivery table with category/status filters.

Do not create another navigation page or another reliability owner.

## Performance boundary

- Successful customer requests execute no new alert code or database query.
- Failed customer requests retain the existing bounded in-memory scheduling.
- AlertEvent database work runs only inside tracked background alert tasks.
- Only actual outbound notifications create AlertEvent rows; dedup-suppressed
  failures remain visible through RequestLog and do not create audit-write load.
- Admin history queries use indexed fields, a maximum page size of 100, and run
  only while the Service Reliability page is active.

## Acceptance criteria

1. Admin API never returns the webhook value and exposes only configured state.
2. Policy updates validate ranges, persist through `SystemSetting`, apply
   immediately, and refresh on other replicas.
3. Successful, failed, and test DingTalk deliveries create sanitized AlertEvent
   history rows.
4. Service Reliability renders policy state and recent deliveries and can send
   one labelled configuration test.
5. Request-path alert scheduling remains non-blocking and bounded.
6. Existing reliability, fallback, routing, billing, and public API behavior
   remains compatible.

## Non-goals

- Webhook editing or viewing in the admin UI
- alert acknowledgement/escalation workflows
- retrying historical failed deliveries
- replacing RequestLog, Redis burst counters, or router cooldown state
- alerting on payment, worker, or quota-service events in this slice

## Retirement and rollback

There is no old admin alert owner to retire. Rollback removes the new admin
router/UI and stops creating AlertEvent rows; existing rows may remain as inert
audit history. Environment-based alerting and RequestLog remain functional.
