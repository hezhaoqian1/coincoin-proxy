# Enterprise Reporting API And Admin Management

Date: `2026-07-28`
Status: `proposed`

## Goal

Give an enterprise customer one or more dedicated read-only credentials that
can inspect the balances and aggregate usage of an administrator-approved set
of CoinCoin users. Administrators manage the enterprise, its visible accounts,
and its reporting credentials from a new `Enterprise API` page in the existing
admin console.

The first rollout targets a customer with one high-volume account and multiple
numbered accounts, but the contract must not encode customer names, username
prefixes, email domains, or production user IDs.

Approval source: the user approved a company-level account list inherited by
multiple reporting keys and requested design plus implementation.

## Problem And Evidence

CoinCoin already exposes `/v1/balance` and `/v1/usage`, but each request is
bound to the single user that owns the supplied API or dashboard session key.
An enterprise operator who manages many independent users therefore has no
safe aggregate view.

The current alternatives are not acceptable:

- sharing the admin token grants global read/write authority and exposes
  recoverable customer API keys;
- collecting every model-invocation key creates unnecessary secret sprawl and
  cannot express a centrally managed account set;
- inferring membership from username prefixes or email domains can include
  stale or personal accounts;
- using station membership would also change pricing, model aliases,
  commissions, and reseller behavior.

## Confirmed Product Decisions

1. An enterprise owns one canonical visible-account list.
2. Every active reporting key for that enterprise inherits the same list.
3. An enterprise may hold multiple active keys to support separate production
   integrations and zero-downtime rotation.
4. Reporting keys are read-only and cannot invoke models, recharge accounts,
   manage users, or enter the admin console.
5. Membership is explicit and administrator-controlled. It is never inferred
   from a name, email domain, or query parameter supplied by the customer.
6. The feature remains in the Python gateway and reuses the canonical billing
   implementation. A separate service is not introduced in this slice.

## Options Considered

### Option A: Let The Customer Call Existing User Endpoints

Issue or reuse one normal API key for every visible account and have the
customer call `/v1/balance` repeatedly.

- Advantage: almost no implementation work.
- Problems: distributes model-invocation credentials, has no enterprise-level
  revocation or membership control, and forces customer-side aggregation.
- Decision: rejected.

### Option B: Reuse Stations Or Add A Reporting Kind To `ApiKey`

Use `StationCustomerLink`, or attach a synthetic user to a new `ApiKey.kind`.

- Advantage: fewer tables initially.
- Problems: station membership has pricing and commission semantics, while
  `ApiKey` is canonically user-bound and participates in model authorization.
  Either reuse would blur ownership and create future authorization mistakes.
- Decision: rejected.

### Option C: Dedicated Enterprise, Grant, And Credential Owners

Add an enterprise record, explicit account grants, and dedicated reporting
credentials with a separate authenticator and public router.

- Advantage: clear authority boundary, no model-call capability, explicit
  membership, clean rotation, and straightforward tenant-isolation tests.
- Cost: three small tables and a new router/admin module.
- Decision: recommended and approved in principle.

## Data Model

### `coincoin_enterprise_clients`

| Field | Contract |
| --- | --- |
| `id` | Primary key with `ent_` prefix. |
| `name` | Administrator-facing enterprise name. |
| `code` | Stable, unique public identifier such as `customer-a`. |
| `status` | `active` or `disabled`. |
| `low_balance_threshold_cents` | Enterprise-wide low-balance threshold, default `0`. |
| `created_at`, `updated_at` | Audit timestamps. |

Enterprise records are disabled rather than deleted. Disabling an enterprise
invalidates all of its reporting keys immediately.

### `coincoin_enterprise_account_grants`

| Field | Contract |
| --- | --- |
| `id` | Primary key with `eag_` prefix. |
| `enterprise_id` | Owning enterprise. |
| `user_id` | CoinCoin user visible to the enterprise. |
| `account_code` | Customer-facing stable label, unique inside the enterprise; defaults to the authorized user's username when omitted. |
| `status` | `active` or `disabled`. |
| `created_at`, `updated_at` | Audit timestamps. |

`(enterprise_id, user_id)` and `(enterprise_id, account_code)` are unique.
A user may be deliberately linked to more than one enterprise, but only through
separate explicit grants. Public responses expose `account_code`, never the
internal user ID, email, or a separate username field. An administrator may
intentionally expose the explicitly authorized user's username by leaving
`account_code` blank. The resolved value is persisted and does not track later
username changes.

The admin account editor replaces the complete grant set in one transaction.
Removing a grant changes all enterprise keys immediately; there is no
credential-local account cache or fallback list.

An enterprise may have at most 200 active grants in V1. The admin API rejects
a larger replacement set with `422`, which keeps public balance reads and
usage aggregation predictably bounded.

### `coincoin_enterprise_access_keys`

| Field | Contract |
| --- | --- |
| `id` | Primary key with `ek_` prefix. |
| `enterprise_id` | Owning enterprise. |
| `name` | Administrator-facing key name. |
| `key_hash` | Unique peppered hash used for authentication. |
| `status` | `active` or `revoked`. |
| `ip_allowlist` | Optional normalized JSON list of IP addresses or CIDRs. |
| `expires_at` | Optional UTC expiry. |
| `last_used_at` | Last successful authentication time. |
| `created_at` | Creation time. |

Reporting keys use a distinct `cc_ent_` prefix and at least 256 bits of random
material. The plaintext is returned exactly once from the create endpoint and
is never encrypted or stored. Later admin reads return only a fingerprint.

Multiple active keys are allowed. Rotation creates a replacement key, allows a
short overlap, then revokes the old key. A key is never silently overwritten.

## Ownership And Module Boundaries

Create `app/enterprise_reporting.py` as the canonical owner for:

- enterprise reporting-key generation and authentication;
- enterprise account-scope resolution;
- public balance and usage-summary endpoints;
- enterprise-specific admin API operations.

Keep the following existing owners unchanged:

- `app/billing.py`: canonical available-balance calculation;
- `app/usage_buffer.py`: pending local usage cost;
- `app/models.py`: SQLAlchemy persistence declarations;
- `app/security.py`: shared hash primitive and secret-safe utilities;
- `app/admin.py`: general admin operations;
- `app/static/admin.html`: existing admin shell and page wiring.

`app/main.py` performs router registration only. Enterprise reporting must not
copy balance arithmetic, read station retail balances, or call the Go shadow
summary service as a billing source of truth.

## Public Authentication Contract

Enterprise endpoints accept only:

```http
Authorization: Bearer cc_ent_<secret>
```

Authentication performs these checks in order:

1. Header exists and has the enterprise prefix.
2. The peppered hash matches one enterprise access-key row.
3. The key status is active and it has not expired.
4. The owning enterprise status is active.
5. The resolved client IP is allowed when an allowlist is configured.
6. The per-key reporting rate limit permits the request.

Normal CoinCoin API keys, dashboard sessions, the admin token, and an
enterprise key belonging to another enterprise all fail with `401` or `403`.
Authentication failures do not reveal which check failed.

Proxy-derived client IP headers are trusted only when the direct peer matches a
configured trusted-proxy CIDR. With no trusted-proxy configuration, the direct
peer address is authoritative. This avoids presenting a spoofable IP allowlist
as a security control.

The configuration owners are:

- `COINCOIN_TRUSTED_PROXY_CIDRS`: comma-separated proxy CIDRs, empty by
  default; only trusted peers may supply `CF-Connecting-IP` or
  `X-Forwarded-For` client addresses;
- `COINCOIN_ENTERPRISE_REPORTING_RPM`: per-reporting-key request limit,
  default `60` and constrained to a positive integer.

Successful responses include `Cache-Control: no-store`. Authorization headers
and raw reporting keys must never appear in application logs.

## Public API

### `GET /v1/enterprise/balances`

Returns current balance information for every active grant.

```json
{
  "object": "enterprise.balance.list",
  "enterprise": {
    "code": "customer-code",
    "name": "Customer Name"
  },
  "currency": "usd_cents",
  "as_of": "2026-07-28T03:30:00Z",
  "total_available_balance_cents": 540000,
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

Balance values must call `get_available_balance_cents()` and include the local
pending usage cost through `usage_buffer.get_pending_cost()`, matching the
existing `/v1/balance` semantics. The endpoint does not promise a stronger
cross-instance snapshot than the current billing system. `as_of` makes that
freshness boundary explicit.

`balance_status` is:

- `insufficient` when available balance is `<= 0`;
- `low` when it is positive and `<= low_balance_threshold_cents`, when the
  configured threshold is positive;
- `ok` otherwise.

The total is the arithmetic sum of the returned account balances, including
negative balances.

### `GET /v1/enterprise/usage-summary?days=7`

Returns rolling aggregate usage for all active grants. `days` is an integer
from `1` through `90`, defaulting to `7`.

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
    "requests": 70634,
    "input_tokens": 1800000000,
    "output_tokens": 500000000,
    "images": 1,
    "videos": 0,
    "cost_cents": 438422,
    "cost_usd": 4384.22
  },
  "data": [
    {
      "account_code": "primary",
      "requests": 70634,
      "input_tokens": 1800000000,
      "output_tokens": 500000000,
      "images": 1,
      "videos": 0,
      "cost_cents": 438422,
      "cost_usd": 4384.22,
      "last_activity_at": "2026-07-28T03:16:45Z"
    }
  ]
}
```

The query is restricted by the server-resolved granted user IDs before any
aggregation. V1 has no caller-supplied `user_id`, username, key ID, arbitrary
date range, per-request log export, model-routing details, or provider details.

## Admin API

All endpoints use the existing `admin_guard` and never accept an enterprise
reporting key.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/admin/enterprise-clients` | Search and list enterprises with account/key counts. |
| `POST` | `/admin/enterprise-clients` | Create an enterprise. |
| `GET` | `/admin/enterprise-clients/{id}` | Read enterprise, grants, and key fingerprints. |
| `PATCH` | `/admin/enterprise-clients/{id}` | Update name, status, or threshold. |
| `PUT` | `/admin/enterprise-clients/{id}/accounts` | Transactionally replace the visible-account list. |
| `POST` | `/admin/enterprise-clients/{id}/keys` | Create a reporting key and return plaintext once. |
| `PATCH` | `/admin/enterprise-keys/{key_id}` | Update name, expiry, allowlist, or revoke the key. |

The account replacement endpoint validates every user first, rejects duplicate
user IDs and account codes, and makes no changes when any entry is invalid.
There is no public or admin endpoint that retrieves a raw key after creation.

## Admin Page

Add `Enterprise API` under the existing `Management` navigation group.

The first view is a dense enterprise table with:

- enterprise name and code;
- status;
- low-balance threshold;
- authorized-account count;
- active-key count;
- latest successful API access;
- edit action.

Create and edit use one focused modal or drawer with three sections:

1. **Enterprise settings**: name, code, status, and low-balance threshold.
2. **Visible accounts**: searchable user table with checkboxes, current balance,
   last activity, and an editable customer-facing account code.
3. **Reporting keys**: fingerprint, name, status, expiry, allowlist, last use,
   create, copy-once, and revoke actions.

The UI must make these states explicit:

- empty enterprise list;
- no visible accounts;
- no reporting keys;
- newly created key with a one-time copy action;
- revoked or expired key;
- disabled enterprise;
- failed validation or network request.

The raw key is held only in the create response and the current modal state. It
is removed when the modal closes or the page reloads.

## Error Contract

Public endpoints use a small stable envelope:

```json
{
  "error": {
    "type": "authentication_error",
    "code": "invalid_enterprise_key",
    "message": "Invalid enterprise reporting key"
  }
}
```

- `401`: missing, malformed, unknown, revoked, or expired credential.
- `403`: disabled enterprise or IP not allowed.
- `429`: per-key reporting rate limit exceeded.
- `500`: database or billing lookup failure; no partial data is returned.

Admin endpoints use normal FastAPI validation errors and explicit `404`/`409`
responses for missing or conflicting enterprise resources.

## Schema And Compatibility

The three tables are additive. Existing users, API keys, stations, balances,
public endpoints, and billing behavior do not change. Startup `create_all`
creates missing tables; checked-in model definitions and startup migration
behavior remain aligned.

Reporting keys exist only in their dedicated table. This ensures that current
`authenticate_user()` and `authorize_request()` cannot accidentally treat them
as user credentials. Existing `/v1/balance` and `/v1/usage` remain compatible.

Enterprise and key deletion is not supported in V1. Status changes are the
reversible retirement mechanism. Replacing grants is allowed because grants
are authorization state, not financial history.

## Verification

Add a focused `tests/test_enterprise_reporting.py` suite covering:

1. reporting-key creation returns plaintext once and stores no plaintext;
2. normal API keys and session keys cannot call enterprise endpoints;
3. invalid, revoked, expired, IP-blocked, and disabled-enterprise keys fail;
4. two enterprises cannot see each other's granted users;
5. account replacement is atomic and immediately affects every key;
6. account replacement rejects more than 200 grants;
7. balance responses reuse canonical available balance and include pending cost;
8. negative balances and total calculation remain correct;
9. usage aggregation includes only active grants and the requested rolling
   window;
10. responses omit separate username, email, internal user-ID, API-key-ID,
   route, channel, provider, and request-payload fields; `account_code` may
   intentionally equal an authorized user's username;
11. admin CRUD, duplicate validation, key rotation, and revoke behavior;
12. trusted-proxy handling rejects spoofed forwarding headers from untrusted
    peers;
13. admin HTML navigation, loader, account selection, key one-time display, and
    revoke wiring;
14. response headers include `Cache-Control: no-store`.

Run targeted enterprise tests, existing auth/billing/admin tests, the full
backend suite, documentation checks, frontend static checks that cover the
admin HTML, and `git diff --check`.

## Rollout

1. Rotate the existing low-entropy admin token through deployment secrets.
2. Deploy the additive schema and code with no enterprise records configured.
3. Create the launch enterprise from the admin page.
4. Explicitly select the confirmed production accounts. Do not bulk-select by
   prefix or email domain, and do not include stale or personal accounts unless
   the customer confirms them.
5. Set a low-balance threshold and create one production reporting key.
6. Deliver the one-time secret through an approved secure channel.
7. Compare the enterprise response with the admin balances and usage summary.
8. Create a second key and exercise rotation before relying on the integration.
9. Monitor authentication failures, rate limits, and last-used timestamps.

No migration or deployment step automatically creates the launch enterprise,
links production users, issues a live key, or changes customer balances.

## Documentation Impact

- Add the two enterprise endpoints and copy-once credential behavior to the API
  reference.
- Add the two new environment variables to `env.example` and the configuration
  reference.
- Add an operator note covering enterprise creation, account selection, key
  delivery, rotation, disable, and rollback.
- Add a release note before the feature is exposed to a customer.

## Rollback

- Disable the enterprise to reject all reporting requests immediately.
- Revoke individual keys when only one integration is affected.
- Remove the public router registration if the feature must be rolled back in a
  deployment; additive tables and grants may remain for forensic review.
- Existing user API and billing behavior continue unchanged.

## Non-Goals

- Customer self-service membership or key management.
- Per-key account subsets; all keys inherit the enterprise account list.
- Raw request-log export or request-content access.
- Recharge, payment, key creation for user accounts, or any write operation.
- Low-balance webhook delivery in V1.
- SSO, OAuth, mTLS, or a separate reporting service.
- Automatic production customer grouping or key issuance.
- Replacing Python billing with Redis or the Go shadow usage service.

## ADR Signal

This introduces a durable public credential and authorization owner. Record an
ADR after implementation and rollout verification establish the final runtime
boundary. Do not accept an ADR from this unexecuted proposal alone.

## Working Artifacts

### TaskIntentDraft

- Outcome: an administrator can issue a read-only enterprise credential whose
  visible account set is centrally controlled.
- Success evidence: tenant-isolation tests, copy-once credential behavior,
  canonical balance parity, admin UI verification, and public contract tests.
- Stop condition: the additive feature is verified without changing existing
  user, station, or billing behavior.
- Non-goals: no customer self-service, webhook, or separate service.
- Main risks: cross-tenant leakage, accidental model-call authority, secret
  recovery, stale membership, and duplicate balance logic.

### BaselineReadSetHint

- `AGENTS.md`
- `docs/README.md`
- `app/auth.py`, `app/proxy.py`, `app/keys.py`
- `app/billing.py`, `app/usage_buffer.py`
- `app/models.py`, `app/main.py`
- `app/admin.py`, `app/static/admin.html`
- nearest auth, billing, and admin tests

### BaselineUsageDraft

- Required baseline refs: runtime auth, billing, user-key, station-link, admin,
  and startup schema owners.
- Acknowledged before plan refs: repository instructions, documentation map,
  current public balance/usage routes, admin APIs, station semantics, and model
  definitions.
- Missing refs: no current enterprise requirement or architecture baseline
  exists.
- Decision: continue with an additive isolated owner and explicit acceptance
  criteria.

### Requirement Ready Check

- Requirement source refs: customer request relayed by the user and the user's
  approval of an enterprise-level account list with multiple keys.
- Goals and scope refs: this design's goal, confirmed decisions, APIs, and
  non-goals.
- User/scenario refs: CoinCoin administrator and enterprise operations client.
- Acceptance refs: verification and rollout sections.
- Open blocker questions: none in the proposed V1 contract.
- Decision: ready for user review.

### ImpactStatementDraft

- Affected layers: additive persistence, admin APIs, admin UI, public reporting
  API, authentication, tests, and reference documentation.
- Canonical owners: enterprise reporting module for grants and credentials;
  existing billing module for available balance.
- Invariants: no inferred membership, no raw-key recovery, no model-call
  authority, no cross-enterprise data, no duplicated balance calculation.
- Compatibility: existing user keys, stations, billing, and public routes are
  unchanged.
- Non-goals: no production data mutation during implementation.

### Existence Check

- Proposed new surface: enterprise, account-grant, and reporting-credential
  persistence plus a dedicated router/admin owner.
- Existing reuse candidates: `ApiKey`, `StationCustomerLink`, and existing
  single-user balance/usage routes.
- Why insufficient: each candidate carries conflicting identity, model-call,
  reseller, pricing, or single-user semantics.
- Creation proof: the required many-user read scope has no current canonical
  owner and cannot be represented safely by existing entities.
- Entropy impact: one focused owner replaces customer-side key fan-out and
  prevents station/API-key compatibility branches.
- Decision: add with proof.

### Product Risk Lens

- Value: customers can monitor managed account balances without receiving
  global admin authority or many model-invocation secrets.
- Non-goals: write operations and raw log access.
- Trade-off: three explicit tables add schema surface but substantially reduce
  authorization ambiguity.
- Decision needed: approve this written contract before implementation.

### Architecture Integrity Lens

- Invariant: only explicit active grants determine visibility.
- Canonical contract: the enterprise reporting module authenticates and scopes;
  billing computes balances.
- Responsibility overlap: none with stations or normal API keys.
- Higher-level simplification: company membership is changed once and inherited
  by all credentials.
- Falsifier: any public code path that accepts caller-selected internal user IDs
  or treats a reporting key as a normal API key invalidates the design.
- Verdict: coherent additive boundary.

### Baseline Role Alignment

- Product baseline: enterprise-wide read-only visibility controlled by admin.
- Architecture baseline: Python billing remains canonical; enterprise auth is
  additive and isolated.
- Result: aligned.
- Scope: both.

### Plan-Time Complexity Check

- Artifact class: source and test complexity.
- Pressure: `app/admin.py` exceeds 5,000 lines and `app/static/admin.html`
  exceeds 7,000 lines; both are strong pressure signals.
- Recommendation: put enterprise behavior in a new focused Python module and a
  new focused test module. Limit `app/main.py` to router wiring and
  `app/static/admin.html` to the minimum page markup/loader/event wiring that
  the existing admin shell requires. Do not add enterprise business logic to
  `app/admin.py`.
- Budget result: within budget only with the new owner boundary; adding the full
  backend to `app/admin.py` would be over budget.
