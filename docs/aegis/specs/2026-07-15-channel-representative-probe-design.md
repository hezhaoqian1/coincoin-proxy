# Channel Representative Probe Design

Date: `2026-07-15`
Status: `approved for implementation`

## Goal

Make the provider channel the primary reliability object. Each active channel probes exactly one representative upstream model selected from its active routes, while public-model health remains derived from routing coverage and real traffic rather than inheriting the probe result.

## Approved Behavior

1. Each provider channel has at most one active monitor.
2. Automatic selection chooses the active route with the lowest effective priority, then highest effective weight, then stable route id order.
3. The representative model is the route's `upstream_model`; the probe endpoint follows the selected route and channel type.
4. An administrator may override the automatic choice from the channel's active route models. A manual override remains until reset to automatic or until its route selection becomes invalid.
5. Channels without a supported active text route are `unconfigured` for active probing.
6. A probe sends one minimal non-streaming generation request: `Reply with OK.`. It does not call `/models`, does not require the literal output `OK`, and succeeds only for a structurally valid model response without an error payload.
7. Probe state is observational. It never changes channel status, priority, weight, cooldown, route state, or fallback behavior.
8. A channel probe failure affects only the channel reliability state. It must not mark every public model served by that channel as failed.
9. Public-model state uses active route coverage, real request failures, fallback activity, latency, and request-router cooldown state.

## User Experience

- The provider-channel edit dialog shows a `监测模型` selector.
- `自动` displays the current default route model and endpoint.
- Manual choices are limited to unique active route model/endpoint combinations for that channel.
- An invalid manual selection is shown explicitly and recommends resetting to automatic; it is not silently replaced.
- The reliability console is channel-first: channel summary and incidents precede model routing/traffic details.
- The model section is labeled as routing and real-traffic health, not active-probe health.

## Architecture

- `ProviderChannelMonitor` remains the persistence owner for probe configuration, lease state, latest result, history, and rollups.
- `created_by` distinguishes automatic route selection from administrator override. Existing manual monitor APIs remain available.
- Route reconciliation collapses legacy multi-model and per-endpoint automatic monitors to one active monitor per channel and disables redundant automatic monitors without deleting history.
- `app/channel_monitoring.py` owns representative selection and probe execution.
- `app/reliability.py` owns the separation between channel probe health and public-model routing/traffic health.
- `app/channel_router.py` remains the only runtime routing and fallback authority.

## Compatibility

- Provider channel CRUD, route CRUD, model discovery, connection testing, priority, weight, cooldown, request fallback, streaming, billing, OpenAI, Anthropic, and Claude Code behavior remain unchanged.
- Existing monitor/history rows are retained. Legacy `extra_models` data is no longer executed and is normalized away when its monitor is reconciled or edited.
- Manual monitor backend endpoints remain callable but one active monitor per channel is the new invariant.

## Verification

- Unit tests prove deterministic automatic selection, manual override preservation, invalid override handling, legacy monitor collapse, single-request probing, response-shape validation, and no `/models` call.
- Reliability tests prove channel probe failures do not propagate to model state.
- Admin tests prove the selection API validates active route models and exposes monitor state in channel payloads.
- Browser QA covers automatic selection display, manual override, reset to automatic, channel-first reliability rendering, route drawer, mobile layout, and explicit probe.
- Production QA runs one explicit probe on a healthy channel after deployment and confirms routing configuration is unchanged.

## Non-Goals

- Automatic route disabling or priority changes based on probes.
- Active image, video, or embedding generation probes.
- Shared distributed cache for the admin reliability overview.
- Removing retained monitor history or public manual-monitor APIs in this change.

## ADR Signal

This supersedes the route-derived multi-model targeting portion of ADR-0002 while preserving its observation-only and request-hot-path boundaries. ADR-0002 and the reliability baseline must be amended after implementation.
