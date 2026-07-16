# Image JSON Keepalive Implementation Plan

Goal: Keep OpenAI-compatible synchronous image generation and editing requests alive through Cloudflare while preserving the existing synchronous endpoints and async image-job alternatives.

Architecture: Add one transport-only ASGI middleware around the four synchronous image paths. Fast responses pass through unchanged. Slow responses emit JSON-safe whitespace heartbeats, then append the existing final JSON body. Existing image routing, fallback, billing, artifact recording, and async job ownership remain unchanged.

Tech Stack: FastAPI/Starlette ASGI, asyncio, Pydantic settings, unittest, httpx ASGI transport.

Baseline/Authority Refs: `docs/aegis/baseline/service-reliability.md`, current `app/proxy.py`, current `app/image_jobs.py`, and the user-approved Sub2API comparison.

Compatibility Boundary: Preserve `/v1/images/generations`, `/v1/images/edits`, `/openai/v1/images/generations`, and `/openai/v1/images/edits`. Preserve fast-response status codes and headers. After the first heartbeat, HTTP status is necessarily committed as `200`; a late error remains an OpenAI-compatible JSON error body. Async image-job endpoints remain additive and unchanged.

Verification: Focused middleware tests, existing image compatibility tests, full backend suite, frontend build, diff check, credential scan, and a production image request after Railway deploy.

## Requirement Ready Check

- Requirement source: user-approved request to adopt the Sub2API dual-track image pattern.
- Scenario: image upstreams can take 120-200 seconds, Cloudflare closes an idle synchronous response at about 120 seconds, and some users cannot directly reach Railway.
- Acceptance: reachable public CoinCoin domain, periodic downstream bytes before 120 seconds, final JSON remains parseable, fast errors retain their original status, late errors have explicit tested semantics, and async jobs remain available.
- Open blockers: none.
- Decision: `ready`.

## Change Necessity

- Configuration alone cannot send downstream bytes while the current endpoint awaits the complete upstream response.
- Moving users to the Railway domain is insufficient because some Windows/mainland networks time out before receiving HTTP.
- Minimum boundary: one image keepalive middleware, one setting, middleware registration, focused tests, and public/operator documentation.
- Decision: `code-change`.

## Architecture Integrity

- Canonical image behavior owner: unchanged `app/proxy.py` and `app/image_jobs.py`.
- Canonical transport keepalive owner: new `app/image_keepalive.py`, registered once in `app/main.py`.
- No route duplication, no billing duplication, and no automatic conversion of synchronous requests into jobs.
- Disable/rollback trigger: set the interval to `0` or revert the middleware commit.

## Task 1: Define and test the transport contract

Files: `tests/test_image_keepalive.py`.

Why: Prove JSON validity and the unavoidable late-error status tradeoff before implementation.

Impact/Compatibility: Tests cover exact image paths only; non-image traffic must remain untouched.

Verification: `python3 -m unittest tests.test_image_keepalive -v`.

- [x] Add failing tests for disabled mode, fast response pass-through, slow success heartbeats, slow error JSON, non-image bypass, and downstream cancellation on client disconnect.
- [x] Run the focused tests and confirm RED because the middleware does not exist.
- [x] Keep assertions on ASGI messages, response status, headers, heartbeat timing, and final `json.loads` behavior.
- [x] Re-run after implementation and confirm GREEN.
- [x] Commit tests with the implementation as one transport contract change.

## Task 2: Implement configurable JSON whitespace keepalive

Files: `app/image_keepalive.py`, `app/config.py`, `app/main.py`, `tests/test_image_keepalive.py`.

Why: Prevent Cloudflare's idle read timeout without changing the standard image endpoint names or speeding assumptions.

Impact/Compatibility: Fast responses retain original status/headers. Slow responses start `200 application/json`, set no-buffer headers, emit whitespace, and finish with the existing JSON body. Unhandled late exceptions become an OpenAI-compatible JSON error body because the status is already committed.

Verification: focused middleware tests plus relevant image compatibility tests.

- [x] Run the focused tests and confirm RED.
- [x] Implement exact path/method matching, buffered downstream ASGI send, configurable heartbeat interval, disconnect cancellation, and late-exception JSON fallback.
- [x] Register the middleware outside quota/billing middleware so inner owners observe the real response status.
- [x] Run focused and image compatibility tests and confirm GREEN.
- [x] Inspect the diff for duplicate routing, billing, or fallback ownership.

## Task 3: Document, verify, and ship

Files: `env.example`, `README.zh-CN.md`, `coincoin-web/src/pages/Docs.jsx`, relevant build/test files only if verification finds a defect.

Why: Operators and users must understand that heartbeats solve idle connection timeouts, not upstream latency or unreachable Railway networking.

Impact/Compatibility: Add one environment variable and explanatory text. No secrets or deployment credentials are committed.

Verification: full backend tests, frontend build, `git diff --check`, credential scan, push to `master`, then one production request through `coincoin.ai`.

- [x] Document the interval, `0` disable switch, late-error HTTP 200 tradeoff, and async alternative.
- [x] Run focused tests, full backend tests, and frontend production build.
- [x] Run diff and credential scans; review only the task files.
- [x] Commit with concise Conventional Commit messages and integrate current `origin/master`.
- [ ] Push to `master`, wait for Railway deployment, and verify a slow image request no longer returns Cloudflare 524.

## Risks and Rollback

- Once a heartbeat is sent, a later upstream failure cannot preserve a non-200 HTTP status. The JSON error body remains authoritative for slow failures.
- Some clients may enforce their own total timeout even while bytes arrive; those clients should use the async job API or increase read timeout.
- Heartbeats do not make image generation faster and do not fix an unreachable API hostname.
- Rollback is `COINCOIN_IMAGE_NONSTREAM_KEEPALIVE_INTERVAL_SECONDS=0` or reverting the middleware commit. No schema or persistent data migration is involved.
