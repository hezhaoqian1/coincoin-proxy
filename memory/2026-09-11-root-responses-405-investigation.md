# Root `/responses` 405 investigation

## Symptom

The client showed `unexpected status 405 Method Not Allowed` for
`https://coincoin.ai/responses` while reconnecting.

## Root cause

The FastAPI app only exposed Responses POST handlers at `/v1/responses` and
`/openai/v1/responses`. The site root `/responses` was handled by the SPA GET
fallback, so POST requests received `405` with `allow: GET`.

This was reproduced against production on 2026-09-11:

- `POST https://coincoin.ai/responses` returned 405 with `allow: GET`.
- `POST https://coincoin.ai/v1/responses` reached the API and returned the
  expected `401 missing api key` without credentials.

## Fix

Added root `/responses` GET and POST aliases in `app/main.py`, reusing the
existing health and proxy handlers. This keeps authentication, routing,
fallback, and billing behavior in the canonical implementation.

## Verification

`git diff --check` passes. The route test is present at
`tests/test_root_responses_route.py`, but it could not run in this checkout
because the Python environment does not have `fastapi` installed.
