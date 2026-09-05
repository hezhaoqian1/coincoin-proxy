# User Model Routing And Cache Billing Overrides

## Goal

Add two admin-only capabilities to CoinCoin without exposing them to normal users:

1. Per-user backend model override for a public model alias.
2. Per-user cache billing override so cached input can be charged at full price instead of the discounted cached price.

The user-facing model name, model catalog, pricing display, and user-visible billing identity must remain the original public alias. Only admin/debug surfaces may reveal the actual upstream model.

## Why This Exists

Today CoinCoin already supports:

- platform-level public alias -> upstream model remapping
- platform-level public model pricing multipliers, including cache-read multipliers
- request logs that preserve both `customer_model_alias` and `provider_model`

That means the product abstraction already exists. The missing piece is user-scoped exceptions inside the admin user-management workflow.

## Evidence Read-Set

- `coincoin-proxy/app/admin.py`
- `coincoin-proxy/app/static/admin.html`
- `coincoin-proxy/app/router.py`
- `coincoin-proxy/app/openai_compat.py`
- `coincoin-proxy/app/anthropic_compat.py`
- `coincoin-proxy/app/station_runtime.py`
- `coincoin-proxy/app/usage_buffer.py`
- `coincoin-proxy/app/models.py`
- `coincoin-proxy/app/schemas.py`
- `docs/operations/admin-support-playbook.md`
- `docs/architecture/model-catalog-and-billing.md`
- upstream reference: `QuantumNous/New-API`

## Current State Summary

### CoinCoin already has

1. Platform-level alias remap:
   - admin edits the public alias target
   - user still calls the old alias
   - request logs preserve both public alias and real provider model

2. Platform-level pricing override:
   - public model can carry `model_multiplier`, `output_multiplier`, and `cache_read_multiplier`
   - `effective_cached_input_per_million` is already computed and logged

3. User management UI and API:
   - user edit modal exists
   - user detail API exists
   - user patch API exists

### CoinCoin does not yet have

1. User-scoped model routing exceptions.
2. User-scoped cache pricing exceptions.
3. A user-management UI for either of those exceptions.

## What NewAPI Does Well

NewAPI is not a drop-in match for this exact requirement, but it has several useful ideas:

1. Channel-level `model_mapping`
   - remaps public model names to upstream model names
   - supports chained mapping
   - detects mapping cycles

2. More expressive billing ratios
   - `model_ratio`
   - `completion_ratio`
   - `cache_ratio`
   - `create_cache_ratio`
   - group-specific and user-group-specific ratio layers

3. Better separation between:
   - routing identity
   - billing ratio
   - user/group policy

## What NewAPI Has That We Should Learn From

### Adopt now

1. Explicit user-scoped override records instead of ad hoc flags on request code paths.
2. Mapping validation strong enough to reject incompatible targets.
3. Clear precedence order when multiple override layers exist.
4. Future-proof pricing fields that can express more than one hard-coded mode.

### Defer for now

1. Full user-group and token-group matrix.
2. Separate cache creation billing override.
3. Broad policy engine for all price dimensions.

Those are good future directions but too large for this change.

## Product Scope

### In scope

1. Admin-only per-user backend model override.
2. Admin-only per-user cache-read full-price override.
3. User-management modal support for both.
4. Runtime application in OpenAI-compatible and Anthropic-compatible request paths.
5. Request-log audit fields that let operators explain what happened later.

### Out of scope

1. Exposing the real backend model to end users.
2. Changing public `/v1/models` output per user.
3. Changing user-visible public alias pricing tables.
4. User-group policy engine.
5. Station alias rewrite rules.

## Compatibility Boundary

### Must remain true

1. Normal users continue to see the original public alias.
2. User-visible charge identity remains the original public alias and SKU family.
3. Admin request logs still show both public alias and actual provider model.
4. Existing platform-level alias overrides continue to work.
5. Existing platform-level model pricing overrides continue to work.
6. Station reseller display alias and pricebook behavior continue to work.

### Visibility rule

Only admin/debug surfaces may expose:

- effective provider model
- user-scoped override source
- user-scoped cache billing override status

## Canonical Owners

### Canonical runtime owners

- `coincoin-proxy/app/router.py`
- `coincoin-proxy/app/openai_compat.py`
- `coincoin-proxy/app/anthropic_compat.py`
- `coincoin-proxy/app/station_runtime.py`
- `coincoin-proxy/app/usage_buffer.py`

### Canonical control-plane owners

- `coincoin-proxy/app/models.py`
- `coincoin-proxy/app/admin.py`
- `coincoin-proxy/app/schemas.py`
- `coincoin-proxy/app/static/admin.html`

## Proposed Data Model

Use explicit user-scoped override tables instead of stuffing JSON into `User`.

### 1. User model routing override table

Suggested table: `coincoin_user_model_routing_overrides`

Fields:

- `user_id`
- `public_model_id`
- `provider_model`
- `upstream_model`
- `enabled`
- `updated_by`
- `created_at`
- `updated_at`

Primary key:

- composite `user_id + public_model_id`

Purpose:

- for one user and one public model, quietly swap the backend target
- preserve the same public alias and pricing identity

### 2. User pricing override table

Suggested table: `coincoin_user_model_pricing_overrides`

Fields:

- `user_id`
- `public_model_id`
- `cache_read_multiplier_override`
- `updated_by`
- `created_at`
- `updated_at`

Primary key:

- composite `user_id + public_model_id`

V1 semantic rules:

- `NULL` or no row: follow platform public model pricing
- `1.0`: cache reads are billed at full non-cached input price

Why use a multiplier instead of a boolean:

- supports the immediate use case
- leaves room for VIP or penalty cases later
- matches the existing public-model pricing vocabulary

## Runtime Resolution Model

### Routing precedence

For a request entering with a public model alias:

1. resolve station alias to public model, if the user belongs to a station
2. resolve platform public model from catalog and platform alias override
3. apply user-scoped routing override for that resolved public model
4. dispatch upstream using the user-specific backend target

Important:

- station alias still owns `display_model`
- platform public model still owns user-visible pricing identity
- user override only changes backend target, not display alias

### Pricing precedence

For cost calculation:

1. keep the existing base price source:
   - station retail if station-owned alias is in effect
   - otherwise platform public model price
2. keep existing model/output/image/video multiplier behavior
3. if a user-specific cache-read multiplier exists, replace the effective cached price with:
   - `effective_input_price * cache_read_multiplier_override`
4. otherwise use existing `effective_cached_input_per_million`

V1 target use case:

- `cache_read_multiplier_override = 1.0`
- cached input billed at the same per-million rate as normal input

## Admin UI Design

Put both controls inside the existing user edit modal in user management.

### New section 1: User model routing overrides

Title:

- `模型转发例外`

Layout:

- table or repeater rows inside the user modal

Per row:

- public model selector
- compatible target selector
- enabled toggle
- delete/reset button

Behavior:

- row is admin-only
- options must come from the same compatibility candidate list used by platform alias override logic
- operator should be able to add multiple per-user model exceptions

Help text:

- user still calls the original model alias
- user-facing display and billing identity stay unchanged
- only backend forwarding target changes

### New section 2: User cache billing overrides

Title:

- `缓存计费例外`

Layout:

- one row per public model, or an add-row list matching the routing section

Per row:

- public model selector
- mode selector:
  - `跟随平台默认`
  - `缓存按原价计费`
- optional raw multiplier input hidden in v1 unless needed later

Help text:

- this only changes cached-input billing
- normal input/output prices remain unchanged
- users will still see the same public model and public pricing identity

### Why user management is the right home

The request is explicitly user-scoped, not platform-scoped:

- platform-wide model remap belongs in model-alias admin
- platform-wide pricing belongs in model-pricing admin
- user-specific exceptions belong in user management

## API Design

Keep the existing `/admin/users/{user_id}` detail flow and add child endpoints for explicit records.

### Suggested endpoints

1. `GET /admin/users/{user_id}/model-routing-overrides`
2. `PUT /admin/users/{user_id}/model-routing-overrides/{public_model_id}`
3. `DELETE /admin/users/{user_id}/model-routing-overrides/{public_model_id}`

4. `GET /admin/users/{user_id}/model-pricing-overrides`
5. `PUT /admin/users/{user_id}/model-pricing-overrides/{public_model_id}`
6. `DELETE /admin/users/{user_id}/model-pricing-overrides/{public_model_id}`

Why separate endpoints:

- easier to validate
- easier to audit
- avoids overloading the general user patch payload
- lets the user detail modal load the two override lists independently

## Request Logging And Audit

Current logs already preserve:

- `customer_model_alias`
- `provider_model`
- pricing audit fields

Add more audit fields only if needed for operator clarity:

- `user_routing_override_applied` boolean
- `user_cache_pricing_override_applied` boolean
- `effective_cache_read_multiplier` float

These fields are admin/debug only and should not flow to normal user surfaces.

## Station Boundary

This design must not break station behavior.

Current station runtime does:

- station alias -> target public model
- station display alias remains station alias
- station retail pricebook may override the visible retail price

Therefore the user override should attach to the resolved public model after station alias resolution, not to the station alias string itself.

This keeps:

- station display alias unchanged
- station retail pricing unchanged
- backend target still overridable per user

## Error Handling

### Reject invalid routing override when

1. public model does not exist
2. target provider model is not in the compatible target list
3. target creates an invalid route for the requested capability

### Reject invalid pricing override when

1. public model does not exist
2. cache multiplier is negative
3. cache multiplier is missing when a row is being created

## Security And Privacy

1. No end-user API should expose these override records.
2. No public docs should mention real provider target for a user exception.
3. Admin token surfaces must continue to gate all override operations.
4. Request logs remain the operational source for explaining what truly happened.

## Verification Plan

### Automated

Add focused tests for:

1. admin CRUD for user model routing overrides
2. admin CRUD for user pricing overrides
3. request path uses user-specific backend model while preserving display alias
4. cached input billed at full price when user cache override is active
5. station alias + user override coexist correctly

### Manual

1. Open admin user modal for a test user.
2. Add a routing override for one public model.
3. Send a request as that user using the original public model.
4. Confirm:
   - user-facing model remains the original alias
   - admin request log `provider_model` changed
   - cost is still computed under the original public model identity

5. Add cache full-price override for that user and model.
6. Send a request that hits cache.
7. Confirm:
   - cached tokens are still recorded
   - cost reflects full input price for cached reads
   - user-facing alias remains unchanged

## Retirement / Old Path Handling

### Old path stays

1. Platform alias override stays as the platform-wide default layer.
2. Platform model pricing override stays as the platform-wide default pricing layer.
3. Station alias and pricebook logic stay unchanged.

### New path adds one narrower layer

- user-specific exception layer above the existing platform defaults

No existing path is retired in v1.

## Recommended V1

Ship the smallest version that fully satisfies the request:

1. user-scoped backend target override per public model
2. user-scoped cache-read full-price override per public model
3. both controls inside admin user management modal
4. no normal-user visibility changes

## Future Extensions

Good follow-ups after v1:

1. user-group-level routing and pricing policy
2. separate cache creation billing override
3. user-scoped output multiplier or image/video multiplier
4. richer operator audit timeline for override changes
