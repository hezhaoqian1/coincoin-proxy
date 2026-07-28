---
type: release
status: active
owner: platform
audience: [user, developer, operator, agent]
updated: 2026-07-28
canonical_for: enterprise-reporting-api-release-2026-07
---

# 2026-07 Enterprise Reporting API

Status: implemented on the feature branch; not deployed and no production
enterprise or credential has been created by this change.

## Added

- Three additive tables for enterprises, explicit account grants, and hashed
  reporting credentials.
- Administrator APIs and an **Enterprise API** admin page for enterprise,
  account-scope, and key lifecycle management.
- `GET /v1/enterprise/balances` and
  `GET /v1/enterprise/usage-summary?days=7`.
- Bearer-only reporting authentication, per-key limits, optional IP allowlists,
  trusted-proxy handling, expiry, revocation, and copy-once secrets.

## Compatibility

Existing user API keys, dashboard sessions, stations, model calls, billing
arithmetic, `/v1/balance`, and `/v1/usage` are unchanged. Available balance
continues to use the Python billing owner and local pending usage buffer.

## Operator Action

After deployment, configure trusted proxy CIDRs and the reporting RPM, then use
the [enterprise reporting runbook](../operations/enterprise-reporting.md) to
create an enterprise, select accounts explicitly, and issue a key. Production
customer setup and secret delivery are separate authorized operations.

## Rollback

Disable the enterprise, revoke affected keys, or remove the two router
registrations. The additive tables can remain without affecting existing APIs.
