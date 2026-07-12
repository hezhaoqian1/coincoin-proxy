# Pure Credit Pack Billing Implementation Plan

## Goal

Replace all new VPN-style monthly and traffic-pack purchases with three permanent USD credit packs while preserving existing paid monthly entitlements, keeping legacy balances spendable during migration, and providing a dry-run-first path to move positive historical balances and valid traffic packs into the new wallet without value loss or duplication.

## Architecture

- `app/credit_wallet.py` becomes the canonical owner for all new permanent paid credit batches and their debit/refund allocations.
- `UserSubscription` remains a bounded compatibility owner only for already-paid monthly periods. Retirement trigger: no active subscription has `paid_until` after the cutover population's final paid period.
- `TrafficPackBalance` and positive `User.balance` remain read/debit compatibility sources until the operator runs the migration tool. The migration creates idempotent credit batches, zeros or marks the old spendable source, and emits a zero-drift report.
- New payment orders freeze a `credit-v1` catalog version, `credit_purchase` action, and promised cents. Confirmation grants exactly one permanent credit batch keyed by order number.
- Billing reads and debits in this transition order: active legacy monthly entitlement, un-migrated valid legacy traffic packs, permanent credit batches, then unmigrated scalar legacy balance/debt.
- The customer UI exposes one total available USD balance and three new credit products. Legacy monthly details remain visible only for users who still have an active paid period.

## Tech Stack

- Python 3.13, FastAPI, SQLAlchemy async ORM, MySQL/asyncmy
- React 18 + Vite
- `pytest` / `unittest` async test suite
- Existing startup DDL compatibility layer in `app/main.py`

## Baseline / Authority Refs

- Approved product design: `/Users/windupbird/.gstack/projects/hezhaoqian1-coincoin-proxy/windupbird-master-design-20260712-055650.md`
- Current billing owner: `app/billing.py`
- Payment creation/confirmation: `app/payment.py`, `app/payment_common.py`
- Persistence: `app/models.py`, `app/main.py`
- Async debit refund boundary: `app/video_jobs.py`
- Customer UI: `coincoin-web/src/pages/Recharge.jsx`, `coincoin-web/src/api/client.js`
- Tests: `tests/test_subscription_billing.py`, `tests/test_admin_usage_fields.py`, `tests/test_video_jobs.py`

## Compatibility Boundary

- Existing active monthly subscriptions continue to normalize, reset already-paid periods, and debit until their existing `paid_until`.
- No new `monthly_*` or `addon_*` order can be created after this change.
- Existing pending legacy product orders are not auto-confirmed through the new catalog; they remain an explicit manual refund/quarantine responsibility.
- Existing traffic packs remain usable even if the monthly subscription expires, until migrated or their current expiry is reached.
- Existing positive `User.balance` remains usable until migrated. Negative balance remains a debt offset and is not converted into a positive batch.
- No live database migration, deployment, pending-order confirmation, or destructive table/column deletion is authorized by this plan.

## Verification

- Targeted wallet/catalog/payment tests must pass.
- Existing subscription compatibility tests must pass after expectations are updated for no new sales.
- Frontend `npm run build` must pass.
- Go `go test ./...` must pass.
- Full Python suite may retain only the three recorded baseline failures in `tests/test_video_jobs.py` caused by missing `RequestLog.effective_cache_creation_input_per_million`; no new failure is allowed.
- Migration dry-run fixtures must prove integer-cent equality before/after and idempotent repeated execution.

## Aegis Visibility

Planning is required because this slice introduces a new billing source of truth, touches persistent value, keeps a time-bounded compatibility owner, and must prove both migration conservation and old-path retirement rather than merely changing product cards.

## Plan Basis

- Fact: users currently buy monthly products to obtain spendable USD value, and prorated upgrades can grant a full tier difference.
- Fact: the current system has three spendable owners: subscriptions, traffic packs, and `User.balance`.
- Fact: the user approved three permanent products: ¥59.90 → `$100`, ¥199 → `$400`, ¥399 → `$1,000`.
- Assumption: existing prepaid future periods are represented sufficiently by the current subscription normalization behavior; this implementation does not reconstruct historical per-period orders.
- Unknown: production count/exposure of negative scalar balances and ambiguous future prepaid periods. The migration dry-run must report them and refuse apply if unsupported states exist.

## BaselineUsageDraft

- Required baseline refs: approved design, billing/payment/models/main, subscription tests, video refund path, recharge UI.
- Acknowledged before plan refs: all listed required refs.
- Cited in plan refs: all listed required refs.
- Missing refs: production migration dry-run output; authoritative Epay payment timestamp is unavailable and therefore not used.
- Decision: continue with code and dry-run tooling; live apply remains outside scope.

## Requirement Ready Check

- Requirement source refs: approved design and subsequent user-confirmed pricing/terminology.
- Goals and scope refs: pure credit packs, three tiers, old monthly retention, no duration for new paid credits.
- User / scenario refs: new buyer, active legacy monthly user, depleted monthly user, valid legacy traffic-pack owner, pending legacy order.
- Acceptance refs: design success criteria plus this plan's verification section.
- Open blocker questions: none for code implementation; production apply requires dry-run evidence.
- Decision: ready.

## Change Necessity

- User-visible need: customers must buy permanent USD credit without monthly periods, upgrades, or traffic-pack gates.
- No-change option: wording-only changes would leave old product confirmation and debit semantics active.
- Why code is necessary: product validation, payment grant, persistence, debit/refund, migration, admin payloads, and UI all encode the old model.
- Minimum boundary: new wallet owner plus updates to existing billing/payment/admin/UI owners; no live data deletion.
- Decision: code-change.

## Existence Check

- Proposed new surface: `app/credit_wallet.py`, `CreditBalance`, and `CreditAllocation`.
- Existing owner candidate: `app/billing.py`, `User.balance`, `TrafficPackBalance`.
- Why insufficient: `billing.py` is already 718 lines and mixes subscription/catalog/debit serialization; scalar balance has no order provenance; traffic packs require expiry and old semantics.
- Creation proof: permanent batches need idempotent order grants, ordered debit, and exact async refund allocations that no current owner can represent cleanly.
- Entropy / retirement impact: traffic-pack and positive scalar balance main paths retire after migration; subscription compatibility has an explicit final-`paid_until` trigger.
- Decision: add-with-proof.

## Architecture Integrity Lens

- Invariant: one confirmed new payment creates one permanent batch exactly once.
- Canonical owner: `app/credit_wallet.py` for permanent credits; `app/billing.py` orchestrates legacy subscription compatibility and wallet calls.
- Overlap: old monthly remains temporarily active by proven persistent-state dependency; it is not extended to new purchases.
- Higher-level simplification: product catalog exposes only credit products; legacy catalog exists only for historical naming/manual handling.
- Retirement falsifier: if new product confirmation or normal post-migration debit still writes `User.balance` or creates `TrafficPackBalance`, retirement has failed.
- Verdict: proceed with bounded compatibility exception.

## Anti-Entropy Declaration

- Deletion class: code-retirement plus contract-carrying persistence compatibility.
- Old paths: public `monthly_*`/`addon_*` sales, prorated quote/upgrade/reset/renew actions, monthly gate for traffic-pack spend, new-payment writes to scalar balance.
- New canonical owner: credit product catalog and credit wallet.
- Preserved behavior: active paid monthly use, legacy pack/balance spend until migration, payment/referral/finance idempotency.
- Retired behavior: all new monthly/add-on sales and automatic prorated confirmation.
- External boundary touched: yes, payment API and customer billing payload.
- Source-of-truth data risk: possible, but no live destructive operation runs in this task.
- User confirmation required: no for code retirement; yes before any future irreversible live cleanup.

## Retirement Decision

- Path: compat-exception.
- Why: existing paid monthly and balance records are active customer contracts; new sales must stop immediately, while old spend paths retire only after migration/final expiry evidence.
- Non-edits: do not drop tables/columns, delete orders, expire customer value, or run the migration with `--apply` against production.

## Complexity Budget

- Artifact class: source and test complexity.
- Pressure: `app/admin.py` 5403 lines, `app/models.py` 859, `app/billing.py` 718, `tests/test_admin_usage_fields.py` 3366.
- Projected pressure: adding wallet internals to `billing.py` would push it over 800 lines and mix owners.
- Budget: at-risk unless wallet logic and tests are extracted.
- Governance: new focused `app/credit_wallet.py`, new `tests/test_credit_wallet.py`; keep admin edits payload-only and avoid unrelated refactors.

## Execution Readiness View

- Intent Lock: replace only future purchase model with permanent credit packs.
- Scope Fence: billing/product/payment/migration/admin/recharge UI; no deployment or live mutation.
- Baseline Lock: three unrelated video tests already fail; all billing-specific baseline tests pass.
- Approved Behavior: three USD credit packs, permanent purchased value, old monthly preserved, traffic packs no longer sold.
- Owner Constraints: wallet owns permanent batches; billing owns orchestration; payment confirmation uses frozen catalog fields.
- Compatibility Boundary: monthly and unmigrated legacy owners remain temporary.
- Retirement Boundary: no new old-product sales; pack/scalar debit paths retire after successful migration; monthly retires after final paid period.
- Task Batches: persistence/wallet, catalog/payment, debit/refund, migration, admin/UI, regression.
- Test Obligations: RED/GREEN per batch, migration zero-drift, frontend build, full regression comparison.
- Review Gates: source-of-truth check after wallet, retirement reference scan after UI, verification-before-completion.
- Drift Rule: if implementation requires live destructive data cleanup or cannot reconcile legacy value, stop and return to user.
- Evidence Required: exact commands and outputs in `docs/aegis/work/2026-07-12-pure-credit-pack-billing/90-evidence.md`.
- Advisory Boundary: Aegis execution guidance only; not production authorization.

## Task 1: Add permanent-credit persistence and wallet owner

**Files:** create `app/credit_wallet.py`, create `tests/test_credit_wallet.py`, modify `app/models.py`, modify `app/main.py`.

**Why:** New payments need traceable, idempotent, non-expiring balances and async refunds need exact batch allocations.

**Impact/Compatibility:** Additive schema only. Existing tables stay intact.

**Verification:**

```bash
COINCOIN_DATABASE_URL='mysql://test:test@127.0.0.1:3306/test' /Users/windupbird/Documents/Coincoin中转站/coincoin-proxy/.venv/bin/python -m pytest tests/test_credit_wallet.py -q
```

- [ ] Write RED tests for idempotent order grant, available total, FIFO debit, insufficient balance, multi-batch allocation, and allocation refund.
- [ ] Run the test command and confirm failures are missing models/functions, not fixture errors.
- [ ] Add `CreditBalance` and `CreditAllocation` models and restart-safe DDL/index definitions.
- [ ] Implement grant/list/total/debit/refund functions with stable row-lock ordering and integer cents.
- [ ] Run GREEN tests and commit `feat: add permanent credit wallet`.

## Task 2: Replace the public catalog and freeze new order semantics

**Files:** modify `app/billing.py`, `app/payment.py`, `app/payment_common.py`, `app/models.py`, `app/main.py`, `app/schemas.py`, modify `tests/test_subscription_billing.py`, add payment cases in `tests/test_admin_usage_fields.py` or a focused new payment test file if existing helpers cannot stay small.

**Why:** Merely changing UI cards would leave old products purchasable and confirmable.

**Impact/Compatibility:** Historical product names remain serializable for admin history, but only `credit_*` products pass new order validation.

**Verification:**

```bash
COINCOIN_DATABASE_URL='mysql://test:test@127.0.0.1:3306/test' /Users/windupbird/Documents/Coincoin中转站/coincoin-proxy/.venv/bin/python -m pytest tests/test_subscription_billing.py tests/test_admin_usage_fields.py -k 'billing or payment or order or subscription or traffic_pack' -q
```

- [ ] Write RED tests asserting the three products and prices, exact money validation, rejection of `monthly_*`/`addon_*` creation, frozen `credit-v1` order fields, and one-batch confirmation.
- [ ] Run RED and capture expected assertion failures.
- [ ] Split public credit catalog from hidden legacy product metadata; remove proration from public serialization.
- [ ] Add order catalog/action/promised-credit columns; write them at creation and require them at confirmation.
- [ ] Grant the new batch idempotently and preserve finance/referral/station side effects once.
- [ ] Run GREEN and commit `feat: sell permanent credit packs`.

## Task 3: Route availability, debit, and video refunds through the wallet

**Files:** modify `app/billing.py`, `app/video_jobs.py`, `app/models.py`, `app/main.py`, `tests/test_credit_wallet.py`, `tests/test_subscription_billing.py`, `tests/test_video_jobs.py`.

**Why:** A paid wallet that is not used by real requests is not a working product; async failures must restore the exact batches.

**Impact/Compatibility:** Legacy monthly remains first among current sources. Existing traffic packs become spendable without an active monthly subscription. Unmigrated `User.balance` remains last fallback/debt offset.

**Verification:**

```bash
COINCOIN_DATABASE_URL='mysql://test:test@127.0.0.1:3306/test' /Users/windupbird/Documents/Coincoin中转站/coincoin-proxy/.venv/bin/python -m pytest tests/test_credit_wallet.py tests/test_subscription_billing.py tests/test_video_jobs.py -q
```

- [ ] Write RED tests for legacy-monthly → legacy-pack → wallet → scalar fallback order, no monthly gate on valid old packs, and video refund by stored wallet allocations.
- [ ] Run RED and isolate the three known unrelated video constructor failures from new wallet failures.
- [ ] Extend billing availability/debit result with `credit_cents` and allocation payloads while retaining old keys for compatibility.
- [ ] Add video-job wallet debit fields and refund wallet allocations exactly once.
- [ ] Run GREEN targeted cases; commit `refactor: route usage through credit wallet`.

## Task 4: Add dry-run-first legacy migration tooling

**Files:** create `scripts/migrate_legacy_credits.py`, create `tests/test_credit_migration.py`, modify `app/credit_wallet.py` only if shared pure helpers are needed.

**Why:** Existing pack and scalar value must move without manual arithmetic, duplicate grants, or implicit live mutation.

**Impact/Compatibility:** Script defaults to dry-run. `--apply` requires an explicit flag and aborts on unsupported/ambiguous states. This task never runs apply against production.

**Verification:**

```bash
COINCOIN_DATABASE_URL='mysql://test:test@127.0.0.1:3306/test' /Users/windupbird/Documents/Coincoin中转站/coincoin-proxy/.venv/bin/python -m pytest tests/test_credit_migration.py -q
```

- [ ] Write RED tests for positive scalar conversion, valid pack conversion, expired/depleted skip, negative-balance reporting, idempotent rerun, and zero-drift totals.
- [ ] Run RED.
- [ ] Implement deterministic migration plans keyed by `legacy_balance:user_id` and `legacy_traffic_pack:pack_id`.
- [ ] Implement JSON/human dry-run output and transactional apply with source locks, source retirement markers, and post-commit reconciliation.
- [ ] Run GREEN; run script `--help`; commit `feat: add credit migration dry run`.

## Task 5: Update admin/customer payloads and recharge UI

**Files:** modify `app/admin.py`, `app/payment.py`, `app/openai_compat.py`, `app/schemas.py`, `coincoin-web/src/api/client.js`, `coincoin-web/src/pages/Recharge.jsx`, `coincoin-web/src/pages/Recharge.css`, modify focused admin tests.

**Why:** Users must see a simple USD balance and only three permanent products; operators still need legacy detail during retirement.

**Impact/Compatibility:** Keep legacy response keys temporarily where existing clients/tests consume them, but add `credit_wallet` and make `products.credit` canonical. Remove monthly/add-on cards and action copy from the customer page.

**Verification:**

```bash
COINCOIN_DATABASE_URL='mysql://test:test@127.0.0.1:3306/test' /Users/windupbird/Documents/Coincoin中转站/coincoin-proxy/.venv/bin/python -m pytest tests/test_admin_usage_fields.py -k 'billing or finance or user' -q
cd coincoin-web && npm run build
```

- [ ] Write/update RED payload tests for wallet totals, legacy monthly detail, hidden old products, and three credit products.
- [ ] Run RED.
- [ ] Update admin/customer serializers and finance totals to include batches without double counting migrated sources.
- [ ] Replace Recharge product/action UI with three cards and permanent USD copy.
- [ ] Run GREEN and frontend build; commit `feat: update credit purchase experience`.

## Task 6: Retirement and regression verification

**Files:** modify `README.zh-CN.md` only for user-visible billing documentation, update work evidence/checkpoint files.

**Why:** Completion requires proving old sales died, compatibility still works, and no unrelated regression was introduced.

**Verification:**

```bash
rg -n 'purchase_action.*upgrade|本周期补差|流量包仅限|monthly_light|monthly_basic|monthly_flagship|addon_boost|addon_project|addon_ultra' app coincoin-web/src
COINCOIN_DATABASE_URL='mysql://test:test@127.0.0.1:3306/test' /Users/windupbird/Documents/Coincoin中转站/coincoin-proxy/.venv/bin/python -m pytest -q
cd coincoin-web && npm run build
cd ../usage-quota-service && go test ./...
```

- [ ] Add documentation for three permanent products and legacy-monthly behavior.
- [ ] Run lingering-reference scan and classify each remaining reference as required legacy compatibility or stale path; remove stale paths.
- [ ] Run targeted billing/payment/migration/video tests.
- [ ] Run full Python, frontend, and Go regression; compare Python failures to baseline.
- [ ] Run verification-before-completion and record evidence; commit `docs: document permanent credit packs`.

## Risks

- `User.balance` negative debt and positive credit share one scalar; migration must never turn debt into credit.
- Legacy future monthly periods are aggregated in one subscription row. This slice preserves current normalization instead of reconstructing historic schedules; dry-run must report unusual `paid_until` spans.
- Payment callbacks for old pending products lack frozen action metadata. They must be rejected from automatic confirmation rather than guessed.
- Partial migration can double-count old and new sources. Availability excludes sources only after a successful source retirement marker/zeroing in the same transaction.
- Full suite baseline is not green. Evidence must compare exact failure identities, not only counts.

## Retirement

- Public monthly/add-on catalog: retire in Task 2.
- Proration quote and upgrade/reset/renew purchase actions: retire from public/order path in Task 2; legacy helper code may remain only if active subscriptions need normalization, not sales.
- Monthly gate on traffic pack spending: retire in Task 3.
- New payment writes to `User.balance`: retire in Task 2.
- Traffic pack debit path and positive scalar balance debit path: retain until migration evidence proves no active unmigrated source; then remove in a later cleanup slice.
- Subscription debit path: retain until the maximum live `paid_until`; later cleanup requires production confirmation and a new retirement task.
