import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

import app.alert_admin as alert_admin
import app.system_settings as system_settings
from app.config import settings
from app.fallback_alerts import reset_fallback_alert_state
from app.main import app
from app.models import AlertEvent


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self


class _FakeDB:
    def __init__(self, *, execute_results=None):
        self.execute_results = list(execute_results or [])
        self.items = {}
        self.added = []
        self.commit = AsyncMock()

    async def get(self, model, key):
        return self.items.get(key)

    def add(self, item):
        self.added.append(item)
        self.items[item.setting_key] = item

    async def execute(self, statement):
        if not hasattr(self, "queries"):
            self.queries = []
        self.queries.append(statement)
        return self.execute_results.pop(0) if self.execute_results else _RowsResult([])


class AlertAdminApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_admin_token = settings.admin_token
        self.original_webhook = settings.fallback_alert_webhook_url
        settings.admin_token = "admin-secret"
        settings.fallback_alert_webhook_url = "https://oapi.dingtalk.example/robot?access_token=top-secret"
        reset_fallback_alert_state()

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        settings.admin_token = self.original_admin_token
        settings.fallback_alert_webhook_url = self.original_webhook
        reset_fallback_alert_state()

    async def _client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"authorization": "Bearer admin-secret"},
        )

    def _override_db(self, fake_db):
        async def fake_get_db():
            yield fake_db

        app.dependency_overrides[alert_admin.get_db] = fake_get_db

    async def test_config_requires_admin_token(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/admin/alerts/config")

        self.assertEqual(response.status_code, 401, response.text)

    async def test_config_masks_webhook_and_returns_latest_delivery_times(self) -> None:
        fake_db = _FakeDB(
            execute_results=[
                _RowsResult(
                    [
                        ("sent", datetime(2026, 7, 21, 10, 0, 0)),
                        ("failed", datetime(2026, 7, 21, 11, 0, 0)),
                    ]
                )
            ]
        )
        self._override_db(fake_db)

        async with await self._client() as client:
            response = await client.get("/admin/alerts/config")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["webhook_configured"])
        self.assertNotIn("webhook_url", payload)
        self.assertNotIn("top-secret", response.text)
        self.assertEqual(payload["last_success_at"], "2026-07-21T10:00:00")
        self.assertEqual(payload["last_failure_at"], "2026-07-21T11:00:00")

    async def test_patch_persists_complete_policy_and_applies_immediately(self) -> None:
        fake_db = _FakeDB()
        self._override_db(fake_db)
        body = {
            "enabled": True,
            "availability_threshold": 8,
            "authentication_threshold": 4,
            "window_seconds": 90,
            "dedup_seconds": 600,
            "max_pending_tasks": 64,
        }

        async with await self._client() as client:
            response = await client.patch("/admin/alerts/config", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("ON DUPLICATE KEY UPDATE", str(fake_db.queries[0]))
        fake_db.commit.assert_awaited_once()
        self.assertEqual(response.json()["availability_threshold"], 8)
        self.assertEqual(alert_admin.current_alert_policy().max_pending_tasks, 64)

    async def test_patch_rejects_dedup_shorter_than_window(self) -> None:
        self._override_db(_FakeDB())
        body = {
            "enabled": True,
            "availability_threshold": 5,
            "authentication_threshold": 3,
            "window_seconds": 120,
            "dedup_seconds": 60,
            "max_pending_tasks": 64,
        }

        async with await self._client() as client:
            response = await client.patch("/admin/alerts/config", json=body)

        self.assertEqual(response.status_code, 422, response.text)

    async def test_configuration_test_returns_delivery_result_without_webhook(self) -> None:
        self._override_db(_FakeDB())
        with patch.object(
            alert_admin,
            "send_dingtalk_configuration_test",
            AsyncMock(return_value={"sent": True, "event_id": "alt_test"}),
        ):
            async with await self._client() as client:
                response = await client.post("/admin/alerts/test")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"sent": True, "event_id": "alt_test"})
        self.assertNotIn("top-secret", response.text)

    async def test_configuration_test_reports_failed_attempt_for_history(self) -> None:
        self._override_db(_FakeDB())
        with patch.object(
            alert_admin,
            "send_dingtalk_configuration_test",
            AsyncMock(return_value={"sent": False, "event_id": "alt_failed"}),
        ):
            async with await self._client() as client:
                response = await client.post("/admin/alerts/test")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"sent": False, "event_id": "alt_failed"})

    async def test_history_filters_and_caps_results(self) -> None:
        event = SimpleNamespace(
            id="alt_1",
            category="availability",
            severity="critical",
            alert_type="upstream_failure_burst",
            endpoint="messages",
            model="claude-sonnet-4-6",
            channel_id="ch_sixoner",
            status_code=502,
            failure_count=5,
            window_seconds=60,
            request_id="ccreq_1",
            destination_type="dingtalk",
            delivery_status="failed",
            response_status=502,
            error_summary="DingTalk HTTP 502",
            created_at=datetime(2026, 7, 21, 12, 0, 0),
            completed_at=datetime(2026, 7, 21, 12, 0, 1),
        )
        self._override_db(_FakeDB(execute_results=[_RowsResult([event])]))

        async with await self._client() as client:
            response = await client.get(
                "/admin/alerts/events?category=availability&delivery_status=failed&limit=100"
            )
            too_large = await client.get("/admin/alerts/events?limit=101")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["events"][0]["request_id"], "ccreq_1")
        self.assertEqual(too_large.status_code, 422, too_large.text)

    async def test_runtime_setting_state_changes_when_value_changes_in_same_second(self) -> None:
        updated_at = datetime(2026, 7, 21, 12, 0, 0)
        first = SimpleNamespace(
            setting_key="upstream_failure_alert_threshold",
            setting_value="5",
            updated_at=updated_at,
        )
        second = SimpleNamespace(
            setting_key="upstream_failure_alert_threshold",
            setting_value="8",
            updated_at=updated_at,
        )

        first_state = await system_settings.get_runtime_system_settings_db_state(
            _FakeDB(execute_results=[_RowsResult([first])])
        )
        second_state = await system_settings.get_runtime_system_settings_db_state(
            _FakeDB(execute_results=[_RowsResult([second])])
        )

        self.assertNotEqual(first_state, second_state)

    def test_alert_event_indexes_cover_polling_filter_and_sort_shapes(self) -> None:
        indexes = {
            tuple(column.name for column in index.columns)
            for index in AlertEvent.__table__.indexes
        }

        self.assertIn(("delivery_status", "completed_at"), indexes)
        self.assertIn(("category", "created_at"), indexes)
        self.assertIn(("delivery_status", "created_at"), indexes)
        self.assertIn(("category", "delivery_status", "created_at"), indexes)


if __name__ == "__main__":
    unittest.main()
