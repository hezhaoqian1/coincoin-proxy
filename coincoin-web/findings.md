# Findings & Decisions

## Requirements
- Build a backend for CoinCoin and connect the current React frontend to it.
- Backend should support “中转站” capabilities: OpenAI-compatible endpoints (`/v1/chat/completions`, `/v1/responses`, `/v1/models`).
- Backend should support user-facing features already in the web app: register/activate key, balance, usage logs, recharge/pay.
- Add/plan features: config snippet generator, connectivity/latency test, enhanced usage analytics, balance/risk reminders.
- Admin/key management exists separately (user mentioned), and should be supported/extended by backend APIs.

## Research Findings
- Current repo is frontend-only (Vite + React Router).
- Frontend currently calls two hard-coded remote services:
  - `PROXY_BASE = https://clawfather.up.railway.app` for key activation, balance, usage, and (by docs) OpenAI-compatible `/v1/*`.
  - `PAY_BASE = https://web-production-bbf09.up.railway.app` for creating pay order and querying order status.
- Frontend Docs (`src/pages/Docs.jsx`) declares:
  - Auth header: `Authorization: Bearer sk_cc_xxxxx`
  - Endpoints: `POST /v1/chat/completions`, `POST /v1/responses`, `GET /v1/models`, `GET /v1/balance`, `GET /v1/usage?limit&offset`
- Frontend API client (`src/api/client.js`) implements:
  - `POST /v1/keys/activate` (register → returns `api_key`, `user_id` in UI)
  - `GET /v1/balance` (requires auth)
  - `GET /v1/usage` (requires auth)
  - Payment: `POST /api/pay` and `GET /api/order/:outTradeNo` (different service)
- Inspected upstream OpenAPI specs:
  - `https://clawfather.up.railway.app/openapi.json` includes:
    - OpenAI-compatible endpoints: `/v1/chat/completions`, `/v1/responses`, `/v1/models`, `/v1/embeddings`
    - User/self-service: `/v1/keys/activate`, `/v1/balance`, `/v1/usage`
    - Admin: `/admin/users`, `/admin/keys`, `/admin/recharges`, `/admin/users/{id}/request-logs`, `/admin/usage/daily`, `/admin/metrics/summary`
    - Recharge webhook for crediting: `POST /webhook/recharge` (idempotent by `order_id`) and `GET /webhook/recharge/{order_id}`
  - `https://web-production-bbf09.up.railway.app/openapi.json` includes:
    - Create order: `POST /api/pay`
    - Query order: `GET /api/order/{out_trade_no}`, `GET /api/orders`
    - Payment callbacks: `GET /pay/notify` (async notify), `GET /pay/return` (sync browser return)

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| Start with a “wrap then migrate” backend option | Lowest risk: switch frontend to our backend first, then gradually move logic from existing services |
| Keep OpenAI-compatible surface stable (`/v1/*`) | Existing clients (Codex CLI / Continue / Aider) can switch base_url without code changes |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| None yet | - |

## Resources
- `src/api/client.js` (current frontend API contract + base URLs)
- `src/pages/Docs.jsx` (documented public API surface)
- `task_plan.md` (phased plan)

## Visual/Browser Findings
- No browser inspection performed yet.
