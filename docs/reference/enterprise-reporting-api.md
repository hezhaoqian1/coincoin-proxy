---
type: reference
status: active
owner: platform
audience: [user, developer, operator, agent]
updated: 2026-07-28
canonical_for: enterprise-reporting-api-reference
---

# Enterprise Reporting API

The Enterprise Reporting API lets an enterprise read balances and aggregate
usage for the CoinCoin accounts explicitly granted by an administrator. It is
read-only and does not authorize model calls, recharge, user administration,
or request-log access.

For a copy-paste production walkthrough and polling-based balance alerts, see
the [Chinese enterprise reporting usage guide](../guides/enterprise-reporting-api.md).

## Authentication

Send the reporting key only as a Bearer credential:

```http
Authorization: Bearer cc_ent_<secret>
```

Reporting keys are separate from normal CoinCoin API keys. The secret is shown
once when an administrator creates it. CoinCoin stores only a peppered hash.

Successful responses include `Cache-Control: no-store`. Do not put the key in
a URL, query string, source repository, ticket, or application log.

## Account Identity

Every result row has an `account_code` chosen by the administrator. When the
administrator leaves it blank, CoinCoin saves the authorized user's current
username as the code. This intentionally makes that username visible to the
enterprise. Changing the username alone does not update a previously saved
value, and the response still omits separate username, email, and internal
user-ID fields.

## Balances

```http
GET /v1/enterprise/balances
```

```bash
curl --fail-with-body \
  -H 'Authorization: Bearer cc_ent_REPLACE_WITH_REPORTING_KEY' \
  https://api.example.com/v1/enterprise/balances
```

Example response:

```json
{
  "object": "enterprise.balance.list",
  "enterprise": {"code": "customer-code", "name": "Customer Name"},
  "currency": "usd_cents",
  "as_of": "2026-07-28T03:30:00Z",
  "total_available_balance_cents": 13838,
  "data": [
    {
      "account_code": "ai-001",
      "account_status": "active",
      "available_balance_cents": 14038,
      "available_balance_usd": 140.38,
      "balance_status": "ok",
      "last_activity_at": "2026-07-28T01:20:22Z"
    }
  ]
}
```

Amounts ending in `_cents` are integer US cents. Negative balances are returned
and included in the total. `balance_status` is `insufficient` for values at or
below zero, `low` for positive values at or below the configured positive
threshold, and `ok` otherwise.

The balance uses the same Python billing calculation and local pending-usage
buffer as `/v1/balance`. `as_of` identifies the response time; this API does not
provide a stronger cross-instance snapshot than normal CoinCoin billing.

## Usage Summary

```http
GET /v1/enterprise/usage-summary?days=7
```

`days` defaults to `7` and must be an integer from `1` through `90`.

```bash
curl --fail-with-body \
  -H 'Authorization: Bearer cc_ent_REPLACE_WITH_REPORTING_KEY' \
  'https://api.example.com/v1/enterprise/usage-summary?days=30'
```

Example response:

```json
{
  "object": "enterprise.usage_summary",
  "enterprise": {"code": "customer-code", "name": "Customer Name"},
  "period": {
    "days": 7,
    "start_at": "2026-07-21T03:30:00Z",
    "end_at": "2026-07-28T03:30:00Z"
  },
  "total": {
    "requests": 120,
    "input_tokens": 180000,
    "output_tokens": 50000,
    "images": 1,
    "videos": 0,
    "cost_cents": 422,
    "cost_usd": 4.22
  },
  "data": [
    {
      "account_code": "ai-001",
      "requests": 120,
      "input_tokens": 180000,
      "output_tokens": 50000,
      "images": 1,
      "videos": 0,
      "cost_cents": 422,
      "cost_usd": 4.22,
      "last_activity_at": "2026-07-28T03:16:45Z"
    }
  ]
}
```

The server resolves the enterprise's active account grants before querying
request logs. The contract has no `user_id`, separate username, API-key ID,
arbitrary date range, model-routing, channel, provider, request payload, or
raw-log field.

## Errors

| Status | Meaning |
| --- | --- |
| `401` | Missing, malformed, unknown, revoked, or expired reporting credential. |
| `403` | The enterprise is disabled or the client IP is outside the key allowlist. |
| `422` | Query validation failed, including `days` outside `1..90`. |
| `429` | The per-key reporting rate limit was exceeded. |
| `500` | A database or billing lookup failed; partial account data is not returned. |

Authentication error bodies intentionally do not identify which credential
check failed.

## Related

- [企业余额与用量查询 API 使用指南](../guides/enterprise-reporting-api.md)
- [Enterprise Reporting Operations](../operations/enterprise-reporting.md)
