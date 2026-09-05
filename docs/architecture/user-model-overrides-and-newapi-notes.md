# User Model Overrides And NewAPI Notes

## What We Shipped In CoinCoin

CoinCoin now supports two admin-only, user-scoped controls in user management:

1. Per-user backend model override for a public model alias.
2. Per-user cached-input billing override, including full-price cache billing with `cache_read_multiplier_override = 1.0`.

These overrides are now wired through:

- OpenAI `responses`
- OpenAI `chat/completions`
- OpenAI `embeddings`
- OpenAI `images/generations`
- OpenAI `images/edits`
- OpenAI `videos/generations`
- Anthropic `messages`

The user-facing alias, `/v1/models`, and billing identity remain public/original.
Only admin/debug/audit surfaces keep the real backend model identity.

## What We Learned From NewAPI

NewAPI still has a broader policy surface than this CoinCoin feature set.

### 1. Channel-level model mapping

NewAPI exposes per-channel `Model Mapping` JSON, so one upstream channel can map
requested model names to different real model names without changing the public
request shape.

Why it matters for CoinCoin:

- it is more expressive than a single platform alias target
- it makes upstream/provider-specific remaps easier to localize
- it can coexist with user/group policy layers

CoinCoin equivalent today:

- platform alias remap
- per-user backend override

Missing compared with NewAPI:

- a general channel-local mapping layer
- mapping conflict / loop validation beyond compatible target checking

Source:

- NewAPI channel management docs describe `Model Mapping` as a JSON mapping from
  user-requested names to actual model names.

## 2. Richer pricing policy layers

NewAPI documents a pricing stack around:

- `ModelRatio`
- `CompletionRatio`
- `GroupRatio`
- user-specific priority over group defaults

The docs also describe:

- upstream ratio sync
- visual bulk editing
- group-based rate differences

Why it matters for CoinCoin:

- our current user override is intentionally narrow and safe
- NewAPI shows a path toward layered cost policy instead of one-off toggles

CoinCoin equivalent today:

- public model multipliers
- station retail pricebooks
- per-user cache-read override

Missing compared with NewAPI:

- user group pricing layers
- per-user non-cache pricing multipliers
- cache creation pricing override
- upstream price/rate sync into local pricing config

## 3. Group-based access and routing

NewAPI groups are used for both access control and pricing isolation. Tokens can
also switch groups, including `auto`-style selection in some workflows.

Why it matters for CoinCoin:

- we already have station ownership and per-user exceptions
- the next natural growth step is a middle layer between "platform default" and
  "single user exception"

CoinCoin equivalent today:

- station alias / pricebook layer
- per-user override layer

Missing compared with NewAPI:

- reusable group policy for multiple users
- group-scoped routing override defaults
- group-scoped cache billing defaults

## 4. Admin ergonomics

Recent NewAPI releases and changelog entries show they continue polishing:

- group-ratio display behavior
- pricing display correctness
- channel/group filters
- admin UI density and scanning quality

Why it matters for CoinCoin:

- our current user modal is now functional for this feature
- the next improvement is making override auditability and bulk editing easier

## What To Consider Next In CoinCoin

If we want to extend this feature further, the next high-value steps are:

1. Add a user-group override layer between platform defaults and per-user rows.
2. Add `cache_create_multiplier_override` alongside cache-read override.
3. Add channel-local parameter overrides for advanced upstream compatibility.
4. Add a safer compatibility validator for future multi-hop mappings.
5. Add admin audit fields like:
   - `user_routing_override_applied`
   - `user_cache_pricing_override_applied`
   - `effective_backend_model`
6. Add bulk import/export for user override rules.

## Recommended Boundary To Keep

Even if we extend this area later, keep these invariants:

1. Ordinary users should continue seeing only the public alias.
2. `/v1/models` should not reveal user-specific backend targets.
3. Billing identity should remain tied to the public model unless we explicitly
   ship a broader pricing-policy feature.
4. Admin/debug logs should continue separating:
   - public alias
   - billable SKU
   - actual provider model

## Current Upstream Evidence

- NewAPI channel docs expose `Model Mapping` and `Parameter Override` in channel
  configuration.
- NewAPI rate-setting docs describe model ratio, completion ratio, group ratio,
  user-specific priority, and upstream ratio sync.
- NewAPI group docs describe group-based channel access and group assignment for
  users/tokens.
- The NewAPI GitHub releases page shows active 2026 releases, so this remains a
  moving upstream worth periodically re-checking before copying behavior.
