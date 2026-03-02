# Task Plan: Build CoinCoin Backend + Web Integration

## Goal
Build a production-grade backend service for CoinCoin (OpenAI-compatible proxy + billing + key management + payments), then wire the existing React frontend to it with a safe, maintainable integration.

## Current Phase
Phase 1

## Phases

### Phase 1: Requirements & Discovery
- [x] Identify current frontend architecture and API dependencies
- [x] Confirm what existing backend(s) already do (proxy vs. billing vs. admin)
- [ ] Define MVP scope for the new backend
- [ ] Document findings in findings.md
- **Status:** in_progress

### Phase 2: Planning & Structure
- [ ] Choose backend stack and repo layout (monorepo vs separate repo)
- [ ] Design API surface (public OpenAI-compatible + user web APIs + admin APIs)
- [ ] Design data model (users, keys, ledger, usage, orders)
- [ ] Define deployment/ops plan (env, secrets, logs, metrics)
- **Status:** pending

### Phase 3: Implementation
- [ ] Scaffold backend (TypeScript, router, config, logging)
- [ ] Implement auth + API key verification
- [ ] Implement OpenAI-compatible proxy endpoints (chat/completions, responses, models)
- [ ] Implement metering + billing (token accounting, ledger)
- [ ] Implement payments integration (create order, webhook/callback, credit balance)
- [ ] Implement admin endpoints (key lifecycle, limits, user ops)
- [ ] Update frontend to use env-based base URLs + new endpoints
- **Status:** pending

### Phase 4: Testing & Verification
- [ ] Add integration tests for proxy + billing correctness
- [ ] Verify streaming, timeouts, retries, and error mapping
- [ ] Verify payment flow idempotency and ledger safety
- [ ] Document test results in progress.md
- **Status:** pending

### Phase 5: Delivery
- [ ] Deploy staging + smoke test
- [ ] Migrate production traffic gradually (feature flag / config switch)
- [ ] Final review + handoff docs
- **Status:** pending

## Key Questions
1. New backend is intended to REPLACE the current Railway `PROXY_BASE` service, or wrap it first and migrate gradually?
2. Preferred backend stack: Node.js (Fastify/Nest) vs Python (FastAPI) vs Go?
3. Do you need full OpenAI API compatibility including streaming (SSE) for `/v1/chat/completions` and `/v1/responses`?
4. Source of truth for token usage/cost: upstream response usage fields, or server-side tokenization?
5. Payment provider specifics: current `PAY_BASE` is a gateway you control, or a third-party? Does it support webhooks?

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Use file-based planning docs | Keep backend build plan stable across long sessions |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
|       | 1       |            |
