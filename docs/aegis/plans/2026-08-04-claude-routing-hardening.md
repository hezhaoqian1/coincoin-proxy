# Claude Routing Hardening Implementation Plan

## Goal

Repair the production Claude Haiku route mismatch, make Claude Messages fallback
cover transient upstream failures including Cloudflare 524, expose unsupported
active routes to administrators, and ship the change with live verification.

## Evidence

- Sixoner accepts `claude-haiku-4-5-20251001` and rejects the configured
  `claude-haiku-4-5-20251016` with HTTP 404.
- HTTP 524 failures reached customers because the compatibility layer only selected
  fallback routes for 429, 502, and 503.
- 86 successfully served its tested Haiku, Sonnet, and Opus model identifiers.
- A representative channel probe does not prove that every configured route model
  exists upstream.

## Compatibility Boundary

- Retry only failures that are normally transient: 408, 429, transport errors, and
  all 5xx statuses. Do not retry ordinary 400, 404, or 422 responses.
- Apply the same status predicate to streaming and non-streaming `/v1/messages`.
- Local connection-pool exhaustion remains terminal because another route uses the
  same local pool, but the failed client request must still be logged.
- Route/model audit is advisory. An unavailable or probe-only model catalog must not
  block route saves or disable routes automatically.
- Do not create 1M aliases for 86 without direct support evidence.

## Canonical Owners

- Claude request fallback and sanitized client errors: `app/anthropic_compat.py`.
- Provider model discovery and route audit: `app/admin.py` and the existing provider
  model admin modal.
- Runtime route choice and multi-hop exclusion: existing `app/channel_router.py` and
  `app/router.py`; no new router owner.
- Production route values: `ProviderChannel` and `ModelChannelRoute` rows managed by
  the protected admin API.
- Operator procedure: `docs/architecture/claude-code-upstream-runbook.md`.

## Change Necessity

Configuration repairs fix the current bad Haiku rows but cannot recover future 408,
500, 504, or 524 failures and cannot expose later model drift. The minimum code
boundary is the existing Anthropic compatibility owner, provider discovery owner,
admin presentation, focused tests, and operator documentation. Decision: code-change.

## Minimality Check

- Reuse the existing fallback loop, RequestLog buffer, `/models` discovery endpoint,
  admin modal, and alert path.
- Add one shared status predicate rather than separate streaming branches.
- Add advisory audit data to the existing discovery response instead of a new job,
  table, or customer-path upstream call.
- No new runtime fallback owner or automatic route mutation is introduced.

## Tasks

- [x] Add regression coverage for 408/429/all-5xx fallback, 404 non-fallback,
  streaming 524, multi-hop recovery, and terminal local pool-timeout logging.
- [x] Implement the shared Claude Messages fallback predicate and logging correction.
- [x] Add advisory active-route model audit to provider discovery and render warnings
  in the existing administrator model modal.
- [x] Update the Claude upstream runbook and release notes.
- [x] Run focused and full backend tests, frontend/static checks, documentation checks,
  compile checks, secret scan, and `git diff --check`.
- [x] Correct the two Sixoner Haiku routes, make fallback priorities deterministic,
  add only verified 86 aliases, and re-query the protected admin APIs.
- [x] Run direct upstream and CoinCoin/Claude Code probes and audit all active route
  identifiers against each upstream model catalog.
- [ ] Review, push, merge, and verify the deployed service.

## Verification Status

- 240 focused Claude, route-audit, and alert tests pass.
- 760 tests outside the two unrelated failing baseline modules pass; two are skipped.
- The complete 798-test run has three pre-existing video RequestLog errors and two
  GPT-5.6 catalog-price assertion failures in files untouched by this change.
- Python compilation, the customer frontend production build, administrator inline
  JavaScript syntax, Markdown link checks, secret scans, and `git diff --check` pass.
- The repository baseline does not track `tests/test_docs_check.py` or
  `scripts/check_docs.py`; their documented commands cannot run on this branch.

## Risks And Rollback

- A transient error retry increases upstream attempts and latency before a terminal
  response, but each attempted channel remains bounded by the existing 16-attempt cap.
- 86 has a five-request concurrency limit. Its 429 response can continue to the next
  configured route; no unverified application-side concurrency owner is added here.
- Route audit depends on the upstream's advertised model catalog and is therefore a
  warning only.
- Code rollback is a merge revert. Production route rollback restores the previous
  priority or route rows through the protected admin API; no destructive schema
  migration is part of this change.
