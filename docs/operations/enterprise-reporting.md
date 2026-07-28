---
type: runbook
status: active
owner: platform
audience: [operator, developer, agent]
updated: 2026-07-28
canonical_for: enterprise-reporting-operations
---

# Enterprise Reporting Operations

This runbook covers administrator-managed enterprise reporting. Reading it does
not grant production authority. Use approved production access and customer
identifiers supplied through the normal operations process.

## Configuration

```dotenv
COINCOIN_ENTERPRISE_REPORTING_RPM=60
COINCOIN_TRUSTED_PROXY_CIDRS=
```

`COINCOIN_TRUSTED_PROXY_CIDRS` is a comma-separated list of direct proxy CIDRs.
CoinCoin trusts `CF-Connecting-IP` or the first `X-Forwarded-For` address only
when the direct peer is in this list. Leave it empty when CoinCoin receives
client connections directly. Never list the public Internet or an unverified
proxy range.

## Create And Grant

1. Open **Management > Enterprise API** in the admin console.
2. Create an enterprise with a stable lowercase code and a non-negative
   low-balance threshold in US cents.
3. Search for each intended CoinCoin user and select it explicitly.
4. Assign every selected user a customer-facing `account_code` unique inside
   the enterprise.
5. Review the final count and save the complete account set.

Do not infer membership from email domains, username prefixes, or naming
similarity. Personal and historical accounts require the same explicit review.
Replacing the account set affects every key for the enterprise immediately.

## Issue And Deliver A Key

1. Enter a purpose-specific key name such as `Production monitoring`.
2. Set an expiry and the narrowest practical IP allowlist when the customer has
   stable egress addresses.
3. Create the key and copy the plaintext from the one-time dialog.
4. Deliver it through an approved secret-sharing channel.
5. Ask the customer to call both reporting endpoints and compare at least one
   account with the normal admin billing view.
6. Close the dialog. The plaintext cannot be retrieved again.

Never send an admin token or normal model-call key as a reporting credential.

## Rotate

1. Create a second reporting key with the desired expiry and allowlist.
2. Deliver and verify the replacement while the old key remains active.
3. Confirm the new key has a recent `last_used_at` value.
4. Revoke the old key.
5. Confirm the old key returns `401` and the replacement still succeeds.

Revoked keys cannot be reactivated. Create a new key if rollback of the client
configuration is necessary.

## Disable And Roll Back

- Revoke one key when only one integration is affected.
- Set the enterprise status to `disabled` to reject every enterprise key
  immediately without changing grants or billing data.
- Remove the enterprise router registration in a deployment rollback if the
  entire surface must be withdrawn. The additive tables may remain for audit.

Disabling or rolling back enterprise reporting does not change normal user API
keys, balances, stations, or model routing.

## Verification Checklist

- The enterprise list contains only explicitly approved accounts.
- Public responses contain `account_code`, not usernames, emails, or user IDs.
- `Cache-Control` is `no-store`.
- A normal CoinCoin API key fails on enterprise endpoints.
- A reporting key fails on model-call and admin endpoints.
- A spoofed forwarding header from an untrusted peer does not bypass the IP
  allowlist.
- Balance samples match the canonical admin billing view within the normal
  pending-buffer freshness boundary.
- Revoked and expired keys return generic authentication failures.
