import unittest
from datetime import date, datetime
from types import SimpleNamespace

import httpx

from app.main import app
import app.admin as admin_module


class _FakeAllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeScalarsCollection:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarsCollection(self._rows)


class _FakeDB:
    def __init__(self, *, execute_results=None, scalar_results=None):
        self._execute_results = list(execute_results or [])
        self._scalar_results = list(scalar_results or [])

    async def execute(self, _query):
        if not self._execute_results:
            raise AssertionError("unexpected execute call")
        return self._execute_results.pop(0)

    async def scalar(self, _query):
        if not self._scalar_results:
            raise AssertionError("unexpected scalar call")
        return self._scalar_results.pop(0)


class AdminUsageFieldTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        app.dependency_overrides.pop(admin_module.get_db, None)
        app.dependency_overrides.pop(admin_module.admin_guard, None)

    async def test_daily_usage_exposes_image_totals(self) -> None:
        usage = SimpleNamespace(
            user_id="u_1",
            day=date(2026, 3, 25),
            tokens_total=12345,
            input_tokens=10000,
            output_tokens=2345,
            images_total=4,
            cost_cents=88,
            requests_total=7,
        )
        user = SimpleNamespace(username="alice", external_id="ext_alice")
        fake_db = _FakeDB(execute_results=[_FakeAllResult([(usage, user)])])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/usage/daily")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload[0]["images_total"], 4)
        self.assertEqual(payload[0]["tokens_total"], 12345)
        self.assertEqual(payload[0]["cost_usd"], 0.88)

    async def test_request_logs_expose_provider_alias_and_usage_units(self) -> None:
        log = SimpleNamespace(
            created_at=datetime(2026, 3, 25, 12, 34, 56),
            endpoint="images/generations",
            model="vertex-gemini-2.5-flash-image",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            image_count=2,
            provider_model="gemini-2.5-flash-image",
            customer_model_alias="gemini-image",
            usage_unit_type="images",
            usage_unit_count=2,
            billable_sku="gemini-image",
            cost_cents=14,
            duration_ms=2100,
            status_code=200,
            route_reason="catalog:gemini-image:direct",
        )
        fake_db = _FakeDB(
            execute_results=[
                _FakeScalarResult(1),
                _FakeScalarsResult([log]),
            ]
        )

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/users/u_1/request-logs")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        item = payload["data"][0]
        self.assertEqual(item["model"], "gemini-image")
        self.assertEqual(item["provider_model"], "gemini-2.5-flash-image")
        self.assertEqual(item["customer_model_alias"], "gemini-image")
        self.assertEqual(item["usage_unit_type"], "images")
        self.assertEqual(item["usage_unit_count"], 2)
        self.assertEqual(item["image_count"], 2)
        self.assertEqual(item["billable_sku"], "gemini-image")

    async def test_summary_metrics_expose_images_today(self) -> None:
        fake_db = _FakeDB(scalar_results=[12, 10, 987654, 45, 6])

        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[admin_module.get_db] = fake_get_db
        app.dependency_overrides[admin_module.admin_guard] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin/metrics/summary")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total_users"], 12)
        self.assertEqual(payload["active_users"], 10)
        self.assertEqual(payload["total_tokens"], 987654)
        self.assertEqual(payload["total_requests_today"], 45)
        self.assertEqual(payload["total_images_today"], 6)


if __name__ == "__main__":
    unittest.main()
