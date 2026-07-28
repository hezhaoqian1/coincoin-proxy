import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request

from app import enterprise_reporting as enterprise
from app import proxy as proxy_module
from app.models import EnterpriseAccessKey, EnterpriseAccountGrant, EnterpriseClient, User


NOW = datetime(2026, 7, 28, 3, 30, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, *, values=None, rows=None):
        self.values = list(values or [])
        self.rows = list(rows or [])

    def scalars(self):
        return self

    def all(self):
        return self.values if self.values else self.rows

    def first(self):
        if self.rows:
            return self.rows[0]
        return self.values[0] if self.values else None

    def scalar_one_or_none(self):
        return self.values[0] if self.values else None


class FakeDB:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.commit_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError(f"unexpected statement: {statement}")
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, value):
        if getattr(value, "created_at", None) is None:
            value.created_at = NOW
        if hasattr(value, "updated_at") and getattr(value, "updated_at", None) is None:
            value.updated_at = NOW


def make_request(ip="203.0.113.10", headers=None):
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/v1/enterprise/balances",
            "raw_path": b"/v1/enterprise/balances",
            "query_string": b"",
            "headers": raw_headers,
            "client": (ip, 12345),
            "server": ("testserver", 443),
        }
    )


def make_enterprise(**overrides):
    values = {
        "id": "ent_example",
        "name": "Example Corp",
        "code": "example",
        "status": "active",
        "low_balance_threshold_cents": 500,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return EnterpriseClient(**values)


def make_key(**overrides):
    values = {
        "id": "ek_example",
        "enterprise_id": "ent_example",
        "name": "Production",
        "key_hash": "a" * 64,
        "status": "active",
        "ip_allowlist": "[]",
        "expires_at": NOW + timedelta(days=30),
        "last_used_at": None,
        "created_at": NOW,
    }
    values.update(overrides)
    return EnterpriseAccessKey(**values)


def make_grant(user_id, account_code, **overrides):
    values = {
        "id": f"eag_{account_code}",
        "enterprise_id": "ent_example",
        "user_id": user_id,
        "account_code": account_code,
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return EnterpriseAccountGrant(**values)


class EnterpriseSchemaTests(unittest.TestCase):
    def test_key_has_256_bits_of_entropy_and_dedicated_prefix(self):
        key = enterprise.generate_enterprise_key()
        self.assertTrue(key.startswith("cc_ent_"))
        self.assertGreaterEqual(len(key), 50)
        self.assertNotEqual(key, enterprise.generate_enterprise_key())

    def test_access_key_table_has_no_plaintext_or_encrypted_secret_column(self):
        columns = set(EnterpriseAccessKey.__table__.columns.keys())
        self.assertIn("key_hash", columns)
        self.assertNotIn("api_key", columns)
        self.assertNotIn("raw_key", columns)
        self.assertNotIn("encrypted_key", columns)

    def test_request_models_forbid_extra_fields_and_duplicate_large_payload_is_checked_later(self):
        with self.assertRaises(ValidationError):
            enterprise.EnterpriseCreateRequest(name="Example", code="example", user_id="usr_hidden")
        payload = enterprise.EnterpriseGrantReplaceRequest(
            accounts=[
                {"user_id": f"usr_{index}", "account_code": f"account-{index}", "status": "disabled"}
                for index in range(201)
            ]
        )
        self.assertEqual(len(payload.accounts), 201)

    def test_ip_allowlist_is_normalized_and_limited(self):
        payload = enterprise.EnterpriseKeyCreateRequest(
            name="Production",
            ip_allowlist=["192.0.2.7", "10.0.0.9/24", "192.0.2.7"],
        )
        self.assertEqual(payload.ip_allowlist, ["192.0.2.7", "10.0.0.0/24"])
        with self.assertRaises(ValidationError):
            enterprise.EnterpriseKeyCreateRequest(name="Production", ip_allowlist=["not-an-ip"])

    def test_forwarded_headers_are_used_only_for_trusted_direct_peers(self):
        headers = {"CF-Connecting-IP": "198.51.100.8", "X-Forwarded-For": "198.51.100.9"}
        with patch.object(enterprise.settings, "trusted_proxy_cidrs", ""):
            self.assertEqual(str(enterprise.resolve_client_ip(make_request(headers=headers))), "203.0.113.10")
        with patch.object(enterprise.settings, "trusted_proxy_cidrs", "203.0.113.0/24"):
            self.assertEqual(str(enterprise.resolve_client_ip(make_request(headers=headers))), "198.51.100.8")


class EnterpriseAdminStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parents[1] / "app" / "static" / "admin.html").read_text()

    def test_navigation_page_and_loader_are_wired(self):
        self.assertIn('data-page="enterprise-api"', self.html)
        self.assertIn('id="page-enterprise-api"', self.html)
        self.assertIn("'enterprise-api': loadEnterprises", self.html)

    def test_admin_endpoints_and_account_replacement_are_wired(self):
        for endpoint in (
            "/admin/enterprise-clients",
            "/admin/enterprise-clients/${encodeURIComponent(currentEnterprise.id)}/accounts",
            "/admin/enterprise-clients/${encodeURIComponent(currentEnterprise.id)}/keys",
            "/admin/enterprise-keys/${encodeURIComponent(keyId)}",
        ):
            self.assertIn(endpoint, self.html)
        self.assertIn("enterprise-account-check:checked", self.html)
        self.assertIn("account_code", self.html)

    def test_one_time_secret_is_cleared_and_never_persisted(self):
        self.assertIn("data.api_key || ''", self.html)
        self.assertIn("function closeEnterpriseSecretModal()", self.html)
        self.assertIn("document.getElementById('enterpriseSecretValue').textContent = '';", self.html)
        self.assertNotIn("localStorage.setItem('enterprise", self.html)

    def test_revoke_and_server_string_escaping_are_present(self):
        self.assertIn("function revokeEnterpriseKey(button)", self.html)
        self.assertIn("JSON.stringify({ status: 'revoked' })", self.html)
        self.assertIn("escapeHtml(item.name)", self.html)
        self.assertIn("escapeHtml(key.fingerprint)", self.html)


class EnterpriseAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reporting_key_is_not_accepted_by_model_authentication(self):
        request = make_request(headers={"Authorization": "Bearer cc_ent_reporting_only"})
        db = FakeDB(FakeResult())

        with patch.object(proxy_module.key_cache, "get", AsyncMock(return_value=None)), patch.object(
            proxy_module, "hash_key", return_value="c" * 64
        ):
            with self.assertRaises(Exception) as raised:
                await proxy_module._resolve_user(request, db)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "invalid api key")
        self.assertIn("coincoin_api_keys", str(db.statements[0]))
        self.assertNotIn("coincoin_enterprise_access_keys", str(db.statements[0]))

    async def test_rejects_missing_or_normal_user_key_before_database_lookup(self):
        for value in (None, "Bearer sk_cc_normal"):
            headers = {"Authorization": value} if value else {}
            db = FakeDB()
            with self.subTest(value=value):
                with self.assertRaisesRegex(Exception, "enterprise credential rejected") as raised:
                    await enterprise.authenticate_enterprise(make_request(headers=headers), db)
                self.assertEqual(raised.exception.status_code, 401)
                self.assertEqual(db.statements, [])

    async def test_rejects_unknown_revoked_expired_and_disabled_credentials(self):
        raw = "cc_ent_test-secret"
        request = make_request(headers={"Authorization": f"Bearer {raw}"})
        cases = [
            (FakeDB(FakeResult()), 401),
            (FakeDB(FakeResult(rows=[(make_key(status="revoked"), make_enterprise())])), 401),
            (
                FakeDB(FakeResult(rows=[(make_key(expires_at=NOW - timedelta(seconds=1)), make_enterprise())])),
                401,
            ),
            (
                FakeDB(FakeResult(rows=[(make_key(), make_enterprise(status="disabled"))])),
                403,
            ),
        ]
        with patch.object(enterprise, "hash_key", return_value="a" * 64), patch.object(
            enterprise, "_utcnow", return_value=NOW
        ):
            for db, expected in cases:
                with self.subTest(expected=expected):
                    with self.assertRaises(Exception) as raised:
                        await enterprise.authenticate_enterprise(request, db)
                    self.assertEqual(raised.exception.status_code, expected)

    async def test_untrusted_forwarding_header_cannot_bypass_key_allowlist(self):
        raw = "cc_ent_test-secret"
        key = make_key(ip_allowlist=json.dumps(["198.51.100.8"]))
        db = FakeDB(FakeResult(rows=[(key, make_enterprise())]))
        request = make_request(
            ip="203.0.113.10",
            headers={"Authorization": f"Bearer {raw}", "X-Forwarded-For": "198.51.100.8"},
        )
        with patch.object(enterprise.settings, "trusted_proxy_cidrs", ""), patch.object(
            enterprise, "hash_key", return_value="a" * 64
        ), patch.object(enterprise, "_utcnow", return_value=NOW):
            with self.assertRaises(Exception) as raised:
                await enterprise.authenticate_enterprise(request, db)
        self.assertEqual(raised.exception.status_code, 403)

    async def test_success_updates_last_use_and_rate_limits_per_key(self):
        raw = "cc_ent_test-secret"
        key = make_key(ip_allowlist=json.dumps(["198.51.100.0/24"]))
        db = FakeDB(FakeResult(rows=[(key, make_enterprise())]))
        request = make_request(
            headers={"Authorization": f"Bearer {raw}", "CF-Connecting-IP": "198.51.100.8"}
        )
        with patch.object(enterprise.settings, "trusted_proxy_cidrs", "203.0.113.0/24"), patch.object(
            enterprise, "hash_key", return_value="a" * 64
        ), patch.object(enterprise, "_utcnow", return_value=NOW), patch.object(
            enterprise.rate_limiter, "allow", AsyncMock(return_value=True)
        ) as allowed:
            context = await enterprise.authenticate_enterprise(request, db)
        self.assertEqual(context.enterprise.code, "example")
        self.assertEqual(key.last_used_at, NOW)
        self.assertEqual(db.commit_count, 1)
        allowed.assert_awaited_once_with("enterprise-reporting:ek_example", 60)

    async def test_rate_limit_failure_is_generic_429(self):
        raw = "cc_ent_test-secret"
        db = FakeDB(FakeResult(rows=[(make_key(), make_enterprise())]))
        request = make_request(headers={"Authorization": f"Bearer {raw}"})
        with patch.object(enterprise, "hash_key", return_value="a" * 64), patch.object(
            enterprise, "_utcnow", return_value=NOW
        ), patch.object(enterprise.rate_limiter, "allow", AsyncMock(return_value=False)):
            with self.assertRaises(Exception) as raised:
                await enterprise.authenticate_enterprise(request, db)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.detail, "enterprise credential rejected")


class EnterpriseReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_balances_use_canonical_billing_pending_cost_and_hide_internal_identity(self):
        grants = [make_grant("usr_one", "ai-001"), make_grant("usr_two", "ai-002")]
        users = [
            User(id="usr_one", username="private-one", email="one@example.com", status="active", balance=1000),
            User(id="usr_two", username="private-two", email="two@example.com", status="blocked", balance=-100),
        ]
        activity = [SimpleNamespace(user_id="usr_one", last_activity_at=NOW - timedelta(hours=1))]
        db = FakeDB(
            FakeResult(values=grants),
            FakeResult(values=users),
            FakeResult(rows=activity),
        )
        auth = enterprise.EnterpriseAuthContext("ek_example", make_enterprise())
        response = SimpleNamespace(headers={})
        with patch.object(
            enterprise.usage_buffer, "get_pending_cost", AsyncMock(side_effect=[10, 20])
        ) as pending, patch.object(
            enterprise,
            "get_available_balance_cents",
            AsyncMock(side_effect=[{"available_cents": 450}, {"available_cents": -120}]),
        ) as billing, patch.object(enterprise, "_utcnow", return_value=NOW):
            result = await enterprise.enterprise_balances(response, auth, db)

        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(result["total_available_balance_cents"], 330)
        self.assertEqual([item["balance_status"] for item in result["data"]], ["low", "insufficient"])
        self.assertEqual([call.args[0] for call in pending.await_args_list], ["usr_one", "usr_two"])
        self.assertEqual([call.kwargs["pending_cost_cents"] for call in billing.await_args_list], [10, 20])
        serialized = json.dumps(result)
        for forbidden in ("usr_one", "usr_two", "private-one", "one@example.com", "api_key_id", "channel_id"):
            self.assertNotIn(forbidden, serialized)

    async def test_usage_summary_is_scoped_to_active_grants_and_aggregates_totals(self):
        grants = [make_grant("usr_one", "primary"), make_grant("usr_two", "secondary")]
        rows = [
            SimpleNamespace(
                user_id="usr_one",
                requests=7,
                input_tokens=100,
                output_tokens=25,
                images=1,
                videos=0,
                cost_cents=345,
                last_activity_at=NOW,
            )
        ]
        db = FakeDB(FakeResult(values=grants), FakeResult(rows=rows))
        auth = enterprise.EnterpriseAuthContext("ek_example", make_enterprise())
        response = SimpleNamespace(headers={})
        with patch.object(enterprise, "_utcnow", return_value=NOW):
            result = await enterprise.enterprise_usage_summary(response, 7, auth, db)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(result["period"]["days"], 7)
        self.assertEqual(result["total"]["requests"], 7)
        self.assertEqual(result["total"]["cost_usd"], 3.45)
        self.assertEqual(result["data"][1]["requests"], 0)
        query = str(db.statements[1])
        self.assertIn("coincoin_request_logs.user_id IN", query)
        self.assertIn("coincoin_request_logs.created_at <=", query)
        self.assertNotIn("username", query)

    async def test_active_grant_query_is_bound_to_authenticated_enterprise(self):
        db = FakeDB(FakeResult(values=[]))
        await enterprise._active_grants(db, "ent_only_this_customer")
        compiled = db.statements[0].compile()
        self.assertIn("ent_only_this_customer", compiled.params.values())
        self.assertIn("coincoin_enterprise_account_grants.enterprise_id", str(db.statements[0]))

    async def test_balance_lookup_fails_closed_instead_of_returning_partial_accounts(self):
        grants = [make_grant("usr_one", "one"), make_grant("usr_missing", "missing")]
        users = [User(id="usr_one", username="one", status="active", balance=100)]
        db = FakeDB(FakeResult(values=grants), FakeResult(values=users))
        auth = enterprise.EnterpriseAuthContext("ek_example", make_enterprise())
        with self.assertRaises(Exception) as raised:
            await enterprise.enterprise_balances(SimpleNamespace(headers={}), auth, db)
        self.assertEqual(raised.exception.status_code, 500)

    async def test_public_route_rejects_days_outside_contract(self):
        app = FastAPI()
        app.include_router(enterprise.public_router)
        app.dependency_overrides[enterprise.authenticate_enterprise] = lambda: enterprise.EnterpriseAuthContext(
            "ek_example", make_enterprise()
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            low = await client.get("/v1/enterprise/usage-summary?days=0")
            high = await client.get("/v1/enterprise/usage-summary?days=91")
        self.assertEqual(low.status_code, 422)
        self.assertEqual(high.status_code, 422)

    def test_public_contract_has_no_caller_selected_user_parameter(self):
        routes = {route.path: route for route in enterprise.public_router.routes}
        for path in ("/v1/enterprise/balances", "/v1/enterprise/usage-summary"):
            parameter_names = {field.name for field in routes[path].dependant.query_params}
            self.assertNotIn("user_id", parameter_names)


class EnterpriseAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_enterprises_returns_counts_without_key_material(self):
        ent = make_enterprise()
        grants = [
            make_grant("usr_one", "one"),
            make_grant("usr_two", "two", status="disabled"),
        ]
        keys = [
            make_key(last_used_at=NOW - timedelta(minutes=5)),
            make_key(id="ek_revoked", status="revoked", last_used_at=NOW),
        ]
        db = FakeDB(
            FakeResult(values=[ent]),
            FakeResult(values=grants),
            FakeResult(values=keys),
        )

        result = await enterprise.list_enterprises(" Example ", db)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["data"][0]["account_count"], 1)
        self.assertEqual(result["data"][0]["active_key_count"], 1)
        self.assertEqual(result["data"][0]["last_used_at"], "2026-07-28T03:30:00Z")
        self.assertNotIn("key_hash", json.dumps(result))
        self.assertIn("coincoin_enterprise_clients.name LIKE", str(db.statements[0]))

    async def test_create_enterprise_returns_detail_and_persists_normalized_fields(self):
        db = FakeDB(
            FakeResult(),
            FakeResult(rows=[]),
            FakeResult(values=[]),
        )
        payload = enterprise.EnterpriseCreateRequest(
            name=" Example Corp ",
            code="example-new",
            low_balance_threshold_cents=1200,
        )

        with patch.object(enterprise, "generate_id", return_value="ent_created"):
            result = await enterprise.create_enterprise(payload, db)

        stored = db.added[0]
        self.assertEqual(stored.id, "ent_created")
        self.assertEqual(stored.name, "Example Corp")
        self.assertEqual(stored.status, "active")
        self.assertEqual(db.commit_count, 1)
        self.assertEqual(result["id"], "ent_created")
        self.assertEqual(result["accounts"], [])
        self.assertEqual(result["keys"], [])

    async def test_get_enterprise_returns_accounts_and_key_fingerprints_only(self):
        ent = make_enterprise()
        grant = make_grant("usr_one", "primary")
        user = User(
            id="usr_one",
            username="operator-one",
            email="operator@example.com",
            external_id="external-one",
            status="active",
        )
        key = make_key()
        db = FakeDB(
            FakeResult(values=[ent]),
            FakeResult(rows=[(grant, user)]),
            FakeResult(values=[key]),
        )

        result = await enterprise.get_enterprise(ent.id, db)

        self.assertEqual(result["accounts"][0]["account_code"], "primary")
        self.assertEqual(result["accounts"][0]["user"]["external_id"], "external-one")
        self.assertEqual(result["keys"][0]["fingerprint"], "sha256:aaaaaaaaaaaa")
        serialized = json.dumps(result)
        self.assertNotIn("key_hash", serialized)
        self.assertNotIn("api_key", serialized)

    async def test_update_enterprise_returns_updated_detail(self):
        ent = make_enterprise()
        db = FakeDB(
            FakeResult(values=[ent]),
            FakeResult(rows=[]),
            FakeResult(values=[]),
        )
        payload = enterprise.EnterpriseUpdateRequest(
            name="Renamed Corp",
            status="disabled",
            low_balance_threshold_cents=2500,
        )

        result = await enterprise.update_enterprise(ent.id, payload, db)

        self.assertEqual(db.commit_count, 1)
        self.assertEqual(result["name"], "Renamed Corp")
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["low_balance_threshold_cents"], 2500)

    async def test_grant_replacement_commits_validated_accounts_and_returns_detail(self):
        ent = make_enterprise()
        user = User(
            id="usr_one",
            username="operator-one",
            email="operator@example.com",
            status="active",
        )
        returned_grant = make_grant("usr_one", "primary")
        db = FakeDB(
            FakeResult(values=[ent]),
            FakeResult(values=["usr_one"]),
            FakeResult(),
            FakeResult(rows=[(returned_grant, user)]),
            FakeResult(values=[]),
        )
        payload = enterprise.EnterpriseGrantReplaceRequest(
            accounts=[{"user_id": "usr_one", "account_code": "primary"}]
        )

        with patch.object(enterprise, "generate_id", return_value="eag_created"):
            result = await enterprise.replace_enterprise_accounts(ent.id, payload, db)

        stored = db.added[0]
        self.assertEqual(stored.id, "eag_created")
        self.assertEqual(stored.enterprise_id, ent.id)
        self.assertEqual(stored.user_id, "usr_one")
        self.assertEqual(stored.account_code, "primary")
        self.assertEqual(db.commit_count, 1)
        self.assertIn("DELETE FROM coincoin_enterprise_account_grants", str(db.statements[2]))
        self.assertEqual(result["accounts"][0]["account_code"], "primary")
        self.assertEqual(result["keys"], [])

    async def test_grant_replacement_rejects_duplicates_and_over_200_active_before_mutation(self):
        ent = make_enterprise()
        duplicate = enterprise.EnterpriseGrantReplaceRequest(
            accounts=[
                {"user_id": "usr_one", "account_code": "one"},
                {"user_id": "usr_one", "account_code": "two"},
            ]
        )
        db = FakeDB(FakeResult(values=[ent]))
        with self.assertRaises(Exception) as duplicate_error:
            await enterprise.replace_enterprise_accounts(ent.id, duplicate, db)
        self.assertEqual(duplicate_error.exception.status_code, 422)
        self.assertEqual(len(db.statements), 1)

        oversized = enterprise.EnterpriseGrantReplaceRequest(
            accounts=[
                {"user_id": f"usr_{index}", "account_code": f"account-{index}"}
                for index in range(201)
            ]
        )
        db = FakeDB(FakeResult(values=[ent]))
        with self.assertRaises(Exception) as limit_error:
            await enterprise.replace_enterprise_accounts(ent.id, oversized, db)
        self.assertEqual(limit_error.exception.status_code, 422)
        self.assertEqual(len(db.statements), 1)

    async def test_grant_replacement_validates_all_users_before_delete(self):
        ent = make_enterprise()
        payload = enterprise.EnterpriseGrantReplaceRequest(
            accounts=[{"user_id": "usr_missing", "account_code": "missing"}]
        )
        db = FakeDB(FakeResult(values=[ent]), FakeResult(values=[]))
        with self.assertRaises(Exception) as raised:
            await enterprise.replace_enterprise_accounts(ent.id, payload, db)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(len(db.statements), 2)
        self.assertEqual(db.commit_count, 0)

    async def test_key_creation_returns_plaintext_once_but_persists_only_hash(self):
        ent = make_enterprise()
        db = FakeDB(FakeResult(values=[ent]))
        payload = enterprise.EnterpriseKeyCreateRequest(name="Production")
        with patch.object(enterprise, "generate_enterprise_key", return_value="cc_ent_one_time_secret"), patch.object(
            enterprise, "hash_key", return_value="b" * 64
        ), patch.object(enterprise, "_utcnow", return_value=NOW):
            result = await enterprise.create_enterprise_access_key(ent.id, payload, db)
        stored = db.added[0]
        self.assertEqual(result["api_key"], "cc_ent_one_time_secret")
        self.assertEqual(stored.key_hash, "b" * 64)
        self.assertNotIn("one_time_secret", stored.key_hash)
        self.assertFalse(hasattr(stored, "encrypted_key"))
        self.assertEqual(result["fingerprint"], "sha256:bbbbbbbbbbbb")

    async def test_revoked_key_cannot_be_reactivated(self):
        key = make_key(status="revoked")
        db = FakeDB(FakeResult(values=[key]))
        payload = enterprise.EnterpriseKeyUpdateRequest(status="active")
        with self.assertRaises(Exception) as raised:
            await enterprise.update_enterprise_access_key(key.id, payload, db)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(db.commit_count, 0)

    async def test_patch_rejects_null_for_required_fields(self):
        ent = make_enterprise()
        enterprise_db = FakeDB(FakeResult(values=[ent]))
        with self.assertRaises(Exception) as enterprise_error:
            await enterprise.update_enterprise(
                ent.id,
                enterprise.EnterpriseUpdateRequest(name=None),
                enterprise_db,
            )
        self.assertEqual(enterprise_error.exception.status_code, 422)

        key = make_key()
        key_db = FakeDB(FakeResult(values=[key]))
        with self.assertRaises(Exception) as key_error:
            await enterprise.update_enterprise_access_key(
                key.id,
                enterprise.EnterpriseKeyUpdateRequest(status=None),
                key_db,
            )
        self.assertEqual(key_error.exception.status_code, 422)

    async def test_admin_routes_reject_enterprise_key_as_admin_token(self):
        app = FastAPI()
        app.include_router(enterprise.admin_router)
        with patch.object(enterprise.settings, "admin_token", "test-admin-token"):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                result = await client.get(
                    "/admin/enterprise-clients",
                    headers={"Authorization": "Bearer cc_ent_not_an_admin"},
                )
        self.assertEqual(result.status_code, 401)


if __name__ == "__main__":
    unittest.main()
