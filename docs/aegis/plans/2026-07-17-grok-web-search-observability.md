# Grok Build Web Search Observability Implementation Plan

Goal: Make the Grok Build onboarding commands configure backend Web Search correctly, continuously verify the real Grok CLI search path through CoinCoin, and expose upstream server-side tool usage in request logs.

Architecture: Keep `RequestLog` as the only durable per-request owner and `UsageBuffer.add()` as the only write path. Normalize the bounded server-side tool counters from Responses `usage`, store them as one nullable JSON column, and expose the same normalized object plus a derived total through user and admin usage APIs. The customer frontend renders a compact summary without introducing tool billing.

Tech Stack: FastAPI, SQLAlchemy/MySQL, React/Vite, Node test runner, Python unittest, official Grok CLI.

Baseline/Authority Refs: Current `app/proxy.py`, `app/usage_buffer.py`, `app/models.py`, `app/openai_compat.py`, `app/admin.py`, `coincoin-web/src/pages/GuideDetail.jsx`, and the verified production Grok CLI request through `https://coincoin.ai/v1`.

Compatibility Boundary: Existing request logs remain valid. Historical `NULL` tool usage returns `{}` and total `0`; no history rewrite occurs. The new fields are additive and do not affect request routing, fallback, token billing, multipliers, or daily aggregates.

Verification: Focused backend and frontend tests, an opt-in real Grok CLI Web Search regression, frontend production build, documentation checks available in this branch, and `git diff --check`.

## Requirement Ready Check

- Requirement source: user-approved scope limited to Grok Build tutorial/config commands, real CLI Web Search regression, and `server_side_tool_usage_details` visibility.
- Acceptance: generated macOS/Linux and Windows configurations set both Grok model selectors and `supports_backend_search`; a real CLI test uses CoinCoin and confirms a persisted Web Search count; user/admin logs expose normalized details and the customer UI shows them.
- Deferred: Brave/Tavily simulation, OAuth/subscription pools, tool-level channel routing, and per-tool billing.
- Open blockers: none for implementation. The live regression skips unless explicitly supplied with a disposable CoinCoin API key.
- Decision: `ready`.

## Architecture Integrity

- Canonical log owner: `RequestLog`.
- Canonical write owner: `UsageBuffer.add()`.
- Canonical upstream extraction: Responses usage parsing in `app/proxy.py`.
- Bounded stored keys: `web_search_calls`, `x_search_calls`, `code_interpreter_calls`, `file_search_calls`, `mcp_calls`, `document_search_calls`, and `image_generation_calls`; values are normalized to non-negative integers and unknown keys are ignored.
- Derived value: `num_server_side_tools_used` is the sum of stored details, not a second persisted owner.
- No destructive migration and no changes to billing ownership.

## Task 1: Fix Grok Build configuration and guide validation

Files: `coincoin-web/src/pages/GuideDetail.jsx`, `coincoin-web/src/pages/Docs.jsx`, `coincoin-web/src/pages/GuideDetail.grok.test.js`.

- [x] Add failing tests for `[models].web_search = "grok-build"`, `supports_backend_search = true`, preservation of unrelated config, and a Web Search verification command on both platforms.
- [x] Update the generated macOS/Linux and PowerShell config editors without replacing unrelated user settings.
- [x] Update the inline Docs example and guide copy to explain backend search and the login-free custom-model path.
- [x] Run the focused Node tests and frontend build.

## Task 2: Persist and expose server-side tool usage

Files: `app/models.py`, `app/main.py`, `app/usage_buffer.py`, `app/proxy.py`, `app/openai_compat.py`, `app/admin.py`, focused backend tests.

- [x] Add failing tests for normalization, buffered request-log persistence payloads, non-stream Responses usage, streamed `response.completed` usage, and user/admin API output.
- [x] Add one nullable JSON column through the model and compatibility migration list.
- [x] Normalize details once in the usage buffer and pass the normalized object from Responses usage extraction.
- [x] Return `{}` and total `0` for old rows; do not add tool counters to billing or daily aggregates.
- [x] Run focused backend tests.

## Task 3: Display tool usage and add the real CLI regression

Files: `coincoin-web/src/pages/Usage.jsx`, `coincoin-web/src/pages/Usage.css`, `tests/test_frontend_usage_filters.py`, new opt-in live test under `tests/`.

- [x] Add failing source-level frontend assertions for the compact tool summary and CSV field.
- [x] Render Web, X, Code, File, MCP, document, and image counters when non-zero; render `-` when none are reported.
- [x] Add an opt-in test that creates an isolated temporary Grok home, writes the official custom-model config, runs a real Web Search prompt through CoinCoin, then polls `/v1/usage` until a matching log reports `web_search_calls > 0`.
- [x] Read base URL and API key only from environment variables, never hard-code or print them, and skip safely when prerequisites are absent.

## Task 4: Verify and hand off

- [x] Run focused backend tests and the Grok guide Node tests.
- [x] Run the frontend production build and available docs checks.
- [x] Confirm the live CLI regression skips without explicit opt-in credentials; defer its post-deploy execution until the new usage field is available in production.
- [x] Run `git diff --check`, inspect the task-only diff, scan for credentials, and prepare a concise Conventional Commit.

Verification evidence:

- `tests.test_openai_compat_defaults`: 100 passed.
- usage/admin/frontend/live-test module set: 116 run, 1 opt-in live test skipped.
- full backend discovery: 624 run, 2 skipped, 3 pre-existing `tests.test_video_jobs` errors caused by `effective_cache_creation_input_per_million` not being a `RequestLog` model field on `origin/master`.
- Grok guide Node tests: 3 passed.
- React/Vite production build: passed (87 modules transformed).
- Aegis workspace check: this plan is indexed; the check remains non-zero because the pre-existing `docs/aegis/plans/2026-07-16-image-json-keepalive.md` is not indexed.

## Risks and Rollback

- Some upstreams may omit or add tool counters. The bounded normalizer treats missing or invalid values as zero and ignores unknown keys.
- A successful Web Search request can consume substantial tokens. The live test is opt-in and should use a disposable key with a small balance.
- Schema rollback is code revert plus optionally retaining the additive nullable column; no paid state or historical log row needs deletion.
