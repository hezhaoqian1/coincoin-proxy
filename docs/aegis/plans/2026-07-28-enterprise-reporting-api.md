# Enterprise Reporting API Implementation Plan

**Goal:** Add an administrator-managed enterprise reporting surface that lets
one or more read-only credentials inspect the balances and aggregate usage of
an explicit shared account list.

**Architecture:** Persist enterprises, account grants, and reporting keys in
three additive tables. A focused `app/enterprise_reporting.py` module owns both
the public reporting router and the enterprise admin router. It authenticates a
dedicated `cc_ent_` credential, resolves grants server-side, and calls the
existing Python billing owner. The existing admin HTML receives only page and
event wiring.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async ORM, MySQL-compatible
startup `create_all`, vanilla admin HTML/JavaScript, Python `unittest`, httpx
ASGI transport, Playwright/browser screenshot verification.

**Baseline/Authority Refs:** `AGENTS.md`, `docs/README.md`,
`docs/aegis/specs/2026-07-28-enterprise-reporting-api-design.md`,
`app/security.py`, `app/proxy.py`, `app/keys.py`, `app/billing.py`,
`app/usage_buffer.py`, `app/models.py`, `app/main.py`, `app/admin.py`,
`app/static/admin.html`, and nearest auth/admin/billing tests.

**Compatibility Boundary:** Existing API/session keys, `/v1/balance`,
`/v1/usage`, station membership, pricing, billing arithmetic, customer state,
and user-facing web flows remain unchanged. Implementation does not configure
a live enterprise, link production users, issue a production key, rotate the
admin token, or deploy.

**TDD Route:**
- Mode: off
- Decision: skipped
- Strict authority: not applicable
- Test posture: post-change regression
- Reason: neither the user nor project requested strict TDD; focused API,
  tenant-isolation, static UI, and compatibility regressions are proportional.
- Verification: new enterprise test module, affected auth/admin/billing tests,
  full backend suite, docs validation, admin browser screenshot, and
  `git diff --check`.

## Scope Check

**Aegis Visibility:** A dedicated plan keeps the new credential owner, public
contract, persistence, admin control, compatibility, and rollback evidence tied
to the approved authorization boundary.

**Plan Basis:** The design spec is approved. The current runtime supports only
single-user balance and usage reads; no enterprise membership or reporting
credential owner exists.

**BaselineUsageDraft:**
- Required baseline refs: approved spec plus current auth, key, billing, usage,
  schema, admin, and startup owners.
- Acknowledged before plan refs: repository guidance, documentation map,
  existing balance and usage endpoints, normal key controls, station semantics,
  admin shell, and startup schema behavior.
- Cited in plan refs: approved spec and file owners named in the header.
- Missing refs: no accepted ADR or current enterprise baseline exists.
- Decision: continue; preserve the ADR signal for post-verification backfill.

**Requirement Ready Check:**
- Requirement source refs: approved design spec and explicit user confirmation
  to implement.
- Goals and scope refs: spec Goal, Confirmed Product Decisions, Public API,
  Admin API, Admin Page, Verification, and Non-Goals.
- User/scenario refs: CoinCoin administrator and enterprise operations client.
- Acceptance refs: spec Verification and Rollout sections.
- Open blocker questions: none.
- Decision: ready.

**Change Necessity:**
- User-visible need: a customer must inspect many separately billed users with
  one centrally controlled read-only credential.
- No-change/non-code option: distributing every user model key is unsafe and
  cannot represent one mutable enterprise scope.
- Why code change is necessary: the account relationship, credential, admin
  lifecycle, and server-side aggregation do not exist.
- Minimum change boundary: additive models/config, one focused router owner,
  main wiring, minimal admin HTML wiring, focused tests, and exact docs.
- Decision: code-change.

**Existence Check:**
- Proposed new surface: enterprise client, grant, reporting credential, and
  focused router owner.
- Existing reuse candidates: `ApiKey`, `StationCustomerLink`, and the single-user
  reporting routes.
- Why insufficient: those owners encode model-call, reseller/pricing, or
  single-user semantics.
- Creation proof: no existing entity safely expresses many-user read-only
  visibility.
- Entropy/retirement impact: three explicit tables avoid compatibility branches
  in normal keys and stations; status-based disable/revoke is reversible.
- Decision: add-with-proof.

**Architecture Integrity Lens:** Explicit grants are the only visibility source;
`app/enterprise_reporting.py` owns scope/auth/aggregation and `app/billing.py`
owns balance calculation. No caller-selected internal user IDs and no fallback
to prefix/domain matching are allowed. Verdict: approved owner boundary is the
higher-level stable path.

**Plan Pressure Test:**
- Owner/contract/retirement: focused owner, additive contract, disable/revoke
  rollback.
- Architecture integrity: no overlap with station or normal key ownership.
- Verification scope: auth failures, tenant isolation, billing parity, admin
  lifecycle, UI wiring, docs, full regressions, browser view.
- Task executability: files and commands are explicit below.
- Pressure result: proceed.

**Complexity Budget:**
- Artifact class: Source Complexity and Test Complexity.
- Target files: new enterprise module/test plus wiring in models, config, main,
  env, admin HTML, and docs.
- Current pressure: `app/admin.py` is over 5,000 lines and
  `app/static/admin.html` is over 7,000 lines.
- Projected post-change pressure: no enterprise backend logic in `app/admin.py`;
  admin HTML receives one bounded page and its loader/actions.
- Budget result: at-risk but governed.
- Planned governance: add a focused backend owner and focused test module;
  classify edits to existing large files as wiring-only.

## Files

- Create `app/enterprise_reporting.py`: public/admin routers, schemas, key
  generation/authentication, IP resolution, grants, balance and usage queries.
- Modify `app/models.py`: add `EnterpriseClient`, `EnterpriseAccountGrant`, and
  `EnterpriseAccessKey` declarations and unique indexes.
- Modify `app/config.py`: add `trusted_proxy_cidrs` and
  `enterprise_reporting_rpm` settings.
- Modify `app/main.py`: import and include the public and admin routers only.
- Modify `app/static/admin.html`: navigation item, enterprise page, modal,
  loader, account selection, key creation/copy/revoke actions.
- Modify `env.example`: document the two enterprise security settings.
- Create `tests/test_enterprise_reporting.py`: focused unit/ASGI/static tests.
- Create `docs/reference/enterprise-reporting-api.md`: exact public contract.
- Create `docs/operations/enterprise-reporting.md`: create, grant, issue,
  rotate, disable, verify, and rollback runbook.
- Create `docs/releases/2026-07-enterprise-reporting-api.md`: user/operator
  impact and rollout status.
- Modify documentation indexes, `README.md`, and `README.zh-CN.md` with concise
  links and endpoint rows.

## Task 1: Add Persistence And Security Configuration

**Files:** `app/models.py`, `app/config.py`, `env.example`.

**Why:** The admin needs a durable enterprise scope and dedicated credentials
that cannot enter normal user authentication.

**Change Necessity:** No existing table can represent a many-user read-only
scope. The minimum persistence boundary is exactly the three approved models.

**Impact/Compatibility:** Additive tables and settings only. Do not alter
`ApiKey`, `StationCustomerLink`, `User`, or current defaults.

**Steps:**

1. Add `EnterpriseClient` with `ent_` ID, unique indexed `code`, active/disabled
   status, non-negative threshold, and timestamps.
2. Add `EnterpriseAccountGrant` with `eag_` ID, enterprise/user indexes,
   `account_code`, active/disabled status, and unique constraints on
   `(enterprise_id, user_id)` and `(enterprise_id, account_code)`.
3. Add `EnterpriseAccessKey` with `ek_` ID, unique indexed `key_hash`, no raw or
   encrypted secret column, status, allowlist JSON, expiry, last-used, and
   timestamps.
4. Add `trusted_proxy_cidrs: str = ""` and
   `enterprise_reporting_rpm: int = 60` to settings and matching
   `COINCOIN_*` examples.
5. Run:

   ```bash
   env PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/pycache \
     COINCOIN_DB_HOST=localhost COINCOIN_DB_NAME=test \
     COINCOIN_DB_USER=test COINCOIN_DB_PASSWORD=test \
     python3 -m unittest tests.test_enterprise_reporting -v
   ```

   Before the test file exists, run an import check instead:

   ```bash
   env PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/pycache \
     COINCOIN_DB_HOST=localhost COINCOIN_DB_NAME=test \
     COINCOIN_DB_USER=test COINCOIN_DB_PASSWORD=test \
     python3 -c 'from app.models import EnterpriseClient, EnterpriseAccountGrant, EnterpriseAccessKey'
   ```

   Expected: exit `0`, no mapping or duplicate-table errors.

## Task 2: Implement The Focused Enterprise Owner

**Files:** create `app/enterprise_reporting.py`; modify `app/main.py`.

**Why:** Public reads and admin lifecycle need one auditable authorization
owner separate from user and station keys.

**Change Necessity:** Route wiring alone cannot provide credential validation,
grant enforcement, or aggregation. The minimum owner is one focused module.

**Impact/Compatibility:** Existing routers remain unchanged. Enterprise keys
live in a separate table and cannot resolve through `authenticate_user()`.

**Steps:**

1. Define Pydantic request schemas with `extra="forbid"` for enterprise create,
   update, grant replacement, key create, and key update. Validate enterprise
   codes with `^[a-z0-9][a-z0-9_-]{1,63}$`, statuses, non-negative thresholds,
   key names, expiry, and at most 50 IP/CIDR entries.
2. Implement `generate_enterprise_key()` as `cc_ent_` plus 32 random bytes in
   URL-safe base64, and hash it with the existing peppered `hash_key()`.
3. Implement CIDR normalization and a client-IP resolver that accepts forwarded
   headers only when the direct peer belongs to `trusted_proxy_cidrs`.
4. Implement a dependency that accepts Bearer only, loads key plus enterprise,
   checks status/expiry/IP/rate limit, updates `last_used_at`, and returns a
   small auth context. Use the same generic public error for invalid secrets.
5. Implement admin list/create/detail/update, transactional full grant
   replacement with a 200-entry cap, copy-once key create, and key update/revoke.
   Reuse `require_admin`; do not import or enlarge `app/admin.py`.
6. Implement `/v1/enterprise/balances`: load active grants and users, batch last
   activity, call `get_available_balance_cents()` with per-user pending cost,
   compute status and total, omit internal identity, and set `no-store`.
7. Implement `/v1/enterprise/usage-summary`: constrain `days` to `1..90`, query
   request-log aggregates only for active grant user IDs, map by account code,
   compute totals, and set `no-store`.
8. Register the public and admin routers in `app/main.py` as wiring-only changes.
9. Run the import check and focused tests from Task 1. Expected: exit `0`.

## Task 3: Add Focused API And Isolation Regression Coverage

**Files:** create `tests/test_enterprise_reporting.py`.

**Why:** Cross-enterprise leakage and credential confusion are the highest-risk
failure modes and need executable evidence.

**Change Necessity:** Existing tests know only user/admin/station credentials;
they cannot prove the new boundary.

**Impact/Compatibility:** Test-only. Use lightweight fake async DB results and
ASGI dependency overrides following current test patterns.

**Steps:**

1. Cover key format, one-time secret response, absence of raw/encrypted storage,
   and fingerprint-only reads.
2. Cover missing/malformed/normal user key, unknown, revoked, expired,
   disabled-enterprise, IP-blocked, spoofed forwarded-header, and rate-limit
   failures.
3. Cover two-enterprise isolation and prove no caller-controlled internal user
   selector exists.
4. Cover duplicate/missing-user and over-200 grant replacement failures before
   mutation, plus immediate scope replacement for multiple keys.
5. Cover canonical balance calls, pending cost, negative totals, threshold
   status, last activity, and forbidden-field omission.
6. Cover rolling usage aggregation, empty grants, active-only grants, and
   `days` validation.
7. Cover all admin CRUD/key rotation/revoke response shapes.
8. Run the exact focused command from Task 1. Expected: all enterprise tests
   pass with no skips caused by missing implementation.

## Task 4: Add The Admin Enterprise API Page

**Files:** `app/static/admin.html`, `tests/test_enterprise_reporting.py`.

**Why:** Operators need a safe workflow to control visible accounts and issue
or revoke credentials without direct database access.

**Change Necessity:** The admin API alone does not satisfy the approved operator
workflow. The existing admin shell is the minimum UI boundary.

**Impact/Compatibility:** Wiring-only addition to the large HTML owner; existing
pages, loaders, modals, and navigation stay intact.

**Steps:**

1. Add a familiar building icon and `Enterprise API` item under Management.
2. Add a stable enterprise page containing search, create, refresh, and the
   specified dense table with status/count/last-used actions.
3. Add one enterprise modal with settings, a searchable checkbox user list and
   editable `account_code`, and a reporting-key table.
4. Add loaders/actions for list, create/update, account replacement, key create,
   one-time copy, update/revoke, empty/loading/error states, and modal cleanup.
5. Escape every server-derived string with `escapeHtml`; never place raw keys in
   persistent storage, URLs, data attributes, or later list responses.
6. Add static assertions for navigation, loader mapping, endpoint paths,
   one-time secret handling, and revoke action.
7. Run focused enterprise tests. Expected: backend and static UI assertions pass.

## Task 5: Publish Exact Reference And Operator Guidance

**Files:** `docs/reference/enterprise-reporting-api.md`,
`docs/reference/README.md`, `docs/operations/enterprise-reporting.md`,
`docs/operations/README.md`, `docs/releases/2026-07-enterprise-reporting-api.md`,
`docs/releases/README.md`, `README.md`, `README.zh-CN.md`, `env.example`.

**Why:** A public credential and operator-managed authorization scope are unsafe
without exact delivery, rotation, verification, and rollback documentation.

**Change Necessity:** Runtime code cannot communicate secret-handling and live
authority boundaries to customers/operators.

**Impact/Compatibility:** Documentation only. Use placeholders and never record
production account IDs, customer keys, balances, or admin secrets.

**Steps:**

1. Document both public endpoints, fields, units, headers, `days` bounds, error
   codes, freshness semantics, and `curl` examples with placeholder keys.
2. Document admin creation, explicit account selection, threshold, secure key
   delivery, second-key rotation, revocation, disable, comparison checks, and
   rollback. State that reading the runbook grants no production authority.
3. Document trusted-proxy CIDR and reporting RPM configuration.
4. Add a release record with schema, compatibility, operator action, and
   verification status.
5. Add discoverability links and concise README endpoint entries.
6. Run:

   ```bash
   python3 -m unittest tests.test_docs_check -v
   python3 scripts/check_docs.py
   ```

   Expected: all documentation tests pass and validator prints
   `Documentation validation passed.`

## Task 6: Verify Behavior, UI, Compatibility, And Diff Scope

**Files:** all task files; no production state.

**Why:** Completion requires evidence that the isolated owner works and existing
public/auth/billing/admin behavior remains stable.

**Change Necessity:** Verification is required because the feature adds a public
security boundary and persistence.

**Impact/Compatibility:** Read-only local checks. Do not call production admin
mutations or create a live DataYes credential.

**Steps:**

1. Run focused tests:

   ```bash
   env PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/pycache \
     COINCOIN_DB_HOST=localhost COINCOIN_DB_NAME=test \
     COINCOIN_DB_USER=test COINCOIN_DB_PASSWORD=test \
     python3 -m unittest tests.test_enterprise_reporting \
       tests.test_proxy_auth_cache tests.test_admin_usage_fields -v
   ```

2. Run the full backend suite with the canonical command from `AGENTS.md`.
   Expected: all tests pass, apart from already documented environment skips.
3. Run documentation validation from Task 5 and `git diff --check`.
4. Open the admin HTML through a local static server, navigate to `Enterprise
   API`, and capture desktop and mobile screenshots. Verify the page is
   nonblank, tables do not overflow incoherently, modal controls fit, and no
   elements overlap.
5. Inspect the final task diff against the pre-existing dirty worktree. Confirm
   no user changes were reverted and no secret/customer data was added.
6. Record any unverified database or live-deployment gap explicitly. Do not
   configure DataYes or rotate production secrets without separate authority.

## Risks And Retirement

- **Cross-tenant leakage:** prevent with server-resolved active grants and tests
  using two enterprises with disjoint users.
- **Credential confusion:** dedicated table/prefix/auth dependency ensures
  reporting keys never enter normal user authorization.
- **False IP protection:** forwarded headers are ignored unless the direct peer
  is configured as trusted.
- **Secret recovery:** reporting key plaintext is never persisted; create is
  copy-once and list/detail are fingerprint-only.
- **Balance drift:** call the existing billing owner and disclose the same local
  pending-buffer freshness boundary as `/v1/balance`.
- **Large enterprise query:** cap active grants at 200 and rate-limit by key.
- **Admin monolith growth:** keep backend logic out of `app/admin.py`; HTML
  change is wiring-only. A future admin frontend extraction is separate work.
- **Rollback:** disable enterprise, revoke keys, or remove router registration;
  additive tables may remain. Existing API and billing continue.
- **Retirement:** no old owner is retired. An accepted ADR is eligible only
  after verified implementation/rollout establishes the final boundary.

## Execution Readiness View

- Intent Lock: one enterprise account list inherited by multiple read-only keys.
- Scope Fence: no self-service, per-key subsets, raw logs, writes, webhook,
  separate service, live customer setup, or deployment.
- Baseline Lock: Python billing remains canonical; stations and normal keys are
  unchanged.
- Approved Behavior: admins manage enterprise/grants/keys; customers read
  balances and rolling usage summaries only.
- Owner/Contract Constraints: new focused module owns enterprise auth/scope;
  billing owns balance; no prefix/domain inference.
- Compatibility Boundary: all current routes and credentials retain behavior.
- Retirement Boundary: status/revoke rollback; no destructive table removal.
- Task Batches: persistence/config, backend owner/tests, admin UI, docs, final
  verification.
- Test Obligations: auth matrix, isolation, canonical balance, usage scope,
  admin lifecycle, UI wiring, docs, full regressions, screenshots.
- Review Gates: focused tests after backend and UI, full suite before completion,
  final diff audit against the dirty worktree.
- Drift/Rewind Rules: if implementation needs station semantics, normal-key
  fallback, caller-selected user IDs, or stored raw keys, stop and return to the
  design rather than adding compatibility logic.
- Evidence Required Before Completion: passing commands, rendered screenshots,
  clean diff check, and explicit remaining live/deployment gap.
- Advisory Boundary: method-pack execution guidance only; not GateDecision,
  PolicySnapshot, or completion authority.
