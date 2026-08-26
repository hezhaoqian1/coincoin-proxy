---
type: plan
status: active
owner: platform
audience: [developer, operator, reviewer]
updated: 2026-08-26
canonical_for: grok-46-public-model-support
---

# Grok 4.6 Public Model Support

## Aegis Visibility

This change adds a public model alias and a production provider route, so the
catalog, billing identity, compatibility metadata, operator guide, and live
control-plane configuration must remain consistent.

## Plan Basis

- User request: expose the Sixoner upstream model `grok-4.6` through CoinCoin.
- Baseline: existing Grok models are `route_only`; `grok-4.5` and `grok-build`
  route through Provider Channel records.
- Upstream evidence: Sixoner model discovery lists `grok-4.6`; a direct probe on
  2026-08-26 returned an upstream HTTP 403 edge response, so activation requires
  a post-deploy route smoke rather than catalog evidence alone.

## Requirement Ready Check

- Decision: ready.
- Acceptance: `grok-4.6` appears in the checked-in and fallback public catalogs,
  resolves as a route-only model for both supported text endpoints, is billed
  under a distinct SKU, and has an isolated Sixoner route without changing
  existing model routes.

## TDD Route

- Mode: off.
- Decision: skipped.
- Strict authority: not applicable.
- Test posture: post-change regression.
- Reason: the user did not request strict test-first development; existing
  catalog and compatibility tests provide the proportional regression surface.
- Verification: targeted Python and frontend tests plus live route audit.

## Change Necessity

- User-visible need: clients must be able to request `grok-4.6` by public model
  id and see it in model discovery.
- No-change option: adding only an admin route would leave the public catalog
  unable to resolve the model and would not establish billing metadata.
- Minimum boundary: catalog, catalog/fallback tests, operator guide, and one
  isolated Sixoner route pair for `chat/completions` and `responses`.
- Decision: code-change plus authorized control-plane configuration.

## File Map

- Modify `config/model_catalog.json`: add the route-only `grok-4.6` alias,
  upstream model id, stable metadata, pricing, and distinct billable SKU.
- Modify `tests/test_model_catalog.py` and
  `tests/test_gateway_catalog_sync.py`: assert the new alias contract and
  pricing without weakening existing Grok coverage.
- Modify `coincoin-web/src/api/client.js` and relevant docs copy: keep offline
  model discovery and user-facing model descriptions consistent.
- Modify `docs/guides/grok-build.md`: document the new public alias and route
  mapping, while retaining the existing `grok-build -> grok-4.5` rule.

## Compatibility and Operations

- Keep `grok-4.5` and `grok-build` unchanged.
- Do not replace, disable, reprioritize, or edit existing channels.
- Create only `grok-4.6` routes on the existing independent `Sixoner Grok`
  channel; map upstream model to `grok-4.6`.
- If live probes continue to return 403, leave the new routes disabled or
  report activation as blocked; do not redirect existing aliases.

## Verification Tasks

1. Run targeted catalog, compatibility, and frontend tests; run `git diff --check`.
2. Deploy the merged branch and confirm the main Railway health endpoint.
3. Query Sixoner model discovery and test `grok-4.6` through the new route pair.
4. Audit all pre-existing provider channels and Grok routes for unchanged
   configuration; smoke `grok-4.5` and `grok-build` if a customer key is
   available.

## Risks and Retirement

- Risk: the upstream advertises `grok-4.6` but the edge layer may reject it;
  live activation is therefore conditional on a successful route smoke.
- Risk: pricing may differ from the current Grok baseline; use the current
  fixed Grok text price only as the explicit CoinCoin price until a new pricing
  source is available.
- Retirement: remove only the `grok-4.6` catalog entry and its dedicated routes
  if Sixoner withdraws the model; retain `grok-4.5`, `grok-build`, and all other
  channel records.
