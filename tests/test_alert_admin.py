import asyncio
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
from app.router import registry as model_registry


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
        self.original_registry_settings = getattr(
            model_registry, "_runtime_system_settings", None
        )
        self.original_registry_version = getattr(
            model_registry, "_runtime_system_settings_version", 0
        )
        settings.admin_token = "admin-secret"
        settings.fallback_alert_webhook_url = "https://oapi.dingtalk.example/robot?access_token=top-secret"
        model_registry.clear_runtime_system_settings()
        reset_fallback_alert_state()

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        settings.admin_token = self.original_admin_token
        settings.fallback_alert_webhook_url = self.original_webhook
        if self.original_registry_settings is None:
            model_registry.clear_runtime_system_settings()
        else:
            model_registry.set_runtime_system_settings(
                self.original_registry_settings,
                version=self.original_registry_version,
            )
        reset_fallback_alert_state()

    async def _client(self, *, raise_app_exceptions: bool = True):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=raise_app_exceptions,
            ),
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
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_config_body_validation_errors_are_sanitized_and_not_cached(self) -> None:
        cases = (
            {},
            {
                "content": b'{"webhook_url":"must-not-echo-malformed"',
                "headers": {"content-type": "application/json"},
            },
            {"json": ["must-not-echo-non-object"]},
        )
        self._override_db(_FakeDB())

        async with await self._client(raise_app_exceptions=False) as client:
            for request_kwargs in cases:
                with self.subTest(request_kwargs=request_kwargs):
                    response = await client.patch(
                        "/admin/alerts/config",
                        **request_kwargs,
                    )

                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(
                        response.json(),
                        {"detail": "invalid alert config"},
                    )
                    self.assertNotIn("must-not-echo", response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_config_returns_complete_effective_webhook_without_caching(self) -> None:
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
        self.assertEqual(
            payload["webhook_url"],
            "https://oapi.dingtalk.example/robot?access_token=top-secret",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(payload["last_success_at"], "2026-07-21T10:00:00")
        self.assertEqual(payload["last_failure_at"], "2026-07-21T11:00:00")

    async def test_patch_persists_complete_policy_and_applies_immediately(self) -> None:
        fake_db = _FakeDB()
        self._override_db(fake_db)
        body = {
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=test-token",
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
        self.assertIn(
            "fallback_alert_webhook_url",
            fake_db.queries[0].compile().params.values(),
        )
        self.assertIn(
            body["webhook_url"],
            fake_db.queries[0].compile().params.values(),
        )
        fake_db.commit.assert_awaited_once()
        self.assertEqual(response.json()["availability_threshold"], 8)
        self.assertEqual(response.json()["webhook_url"], body["webhook_url"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(alert_admin.current_alert_policy().max_pending_tasks, 64)

    async def test_patch_empty_webhook_explicitly_shadows_environment_default(self) -> None:
        fake_db = _FakeDB()
        self._override_db(fake_db)
        body = {
            "webhook_url": "",
            "enabled": True,
            "availability_threshold": 5,
            "authentication_threshold": 3,
            "window_seconds": 60,
            "dedup_seconds": 300,
            "max_pending_tasks": 64,
        }

        async with await self._client() as client:
            response = await client.patch("/admin/alerts/config", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["webhook_url"], "")
        self.assertFalse(response.json()["webhook_configured"])
        self.assertEqual(alert_admin.current_alert_webhook_url(), "")
        self.assertIn("", fake_db.queries[0].compile().params.values())

    async def test_patch_empty_webhook_survives_other_runtime_setting_apply(self) -> None:
        fake_db = _FakeDB()
        self._override_db(fake_db)
        body = {
            "webhook_url": "",
            "enabled": True,
            "availability_threshold": 5,
            "authentication_threshold": 3,
            "window_seconds": 60,
            "dedup_seconds": 300,
            "max_pending_tasks": 64,
        }

        async with await self._client() as client:
            response = await client.patch("/admin/alerts/config", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            model_registry.current_system_settings()["fallback_alert_webhook_url"],
            "",
        )

        await system_settings.apply_runtime_system_setting(
            system_settings.CLAUDE_COMPAT_PROVIDER_KEY,
            "kiro_go",
        )

        self.assertEqual(alert_admin.current_alert_webhook_url(), "")
        self.assertEqual(
            model_registry.current_system_settings()["fallback_alert_webhook_url"],
            "",
        )

    async def test_patch_rejects_non_dingtalk_webhook_urls(self) -> None:
        invalid_urls = (
            "http://oapi.dingtalk.com/robot/send?access_token=must-not-echo",
            "https://example.com/robot/send?access_token=must-not-echo",
            "https://oapi.dingtalk.com/not-robot/send?access_token=must-not-echo",
            "https://oapi.dingtalk.com/robot/send",
            "https://oapi.dingtalk.com/robot/send?access_token=",
            "https://oapi.dingtalk.com/robot/send?access_token=&access_token=must-not-echo",
            "https://oapi.dingtalk.com/robot/send?access_token=must-not-echo&access_token=",
            "https://oapi.dingtalk.com/robot/send?access_token=%20must-not-echo%20",
            "https://oapi.dingtalk.com:not-a-port/robot/send?access_token=must-not-echo",
        )
        for webhook_url in invalid_urls:
            with self.subTest(webhook_url=webhook_url):
                fake_db = _FakeDB()
                self._override_db(fake_db)
                body = {
                    "webhook_url": webhook_url,
                    "enabled": True,
                    "availability_threshold": 5,
                    "authentication_threshold": 3,
                    "window_seconds": 60,
                    "dedup_seconds": 300,
                    "max_pending_tasks": 64,
                }

                async with await self._client() as client:
                    response = await client.patch("/admin/alerts/config", json=body)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("must-not-echo", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                fake_db.commit.assert_not_awaited()

    async def test_patch_sanitizes_webhook_url_parser_errors(self) -> None:
        fake_db = _FakeDB()
        self._override_db(fake_db)
        body = {
            "webhook_url": "https://[oapi.dingtalk.com/robot/send?access_token=must-not-echo-parser",
            "enabled": True,
            "availability_threshold": 5,
            "authentication_threshold": 3,
            "window_seconds": 60,
            "dedup_seconds": 300,
            "max_pending_tasks": 64,
        }

        async with await self._client(raise_app_exceptions=False) as client:
            response = await client.patch("/admin/alerts/config", json=body)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json(), {"detail": "invalid alert config"})
        self.assertNotIn("must-not-echo-parser", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        fake_db.commit.assert_not_awaited()

    async def test_patch_rejects_dedup_shorter_than_window(self) -> None:
        self._override_db(_FakeDB())
        body = {
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=test-token",
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
        self.assertNotIn("test-token", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

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
        self.assertNotEqual(
            too_large.json(),
            {"detail": "invalid alert config"},
        )

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

    async def test_runtime_refresh_preserves_explicit_empty_webhook_override(self) -> None:
        row = SimpleNamespace(
            setting_key="fallback_alert_webhook_url",
            setting_value="",
            updated_at=datetime(2026, 7, 21, 12, 0, 0),
        )

        await system_settings.refresh_runtime_system_settings_from_db(
            _FakeDB(execute_results=[_RowsResult([row])])
        )

        self.assertEqual(alert_admin.current_alert_webhook_url(), "")

    async def test_inflight_old_refresh_cannot_finish_after_newer_patch(self) -> None:
        older_url = "https://oapi.dingtalk.com/robot/send?access_token=old-test-token"
        older_row = SimpleNamespace(
            setting_key="fallback_alert_webhook_url",
            setting_value=older_url,
            updated_at=datetime(2026, 7, 21, 12, 0, 0),
        )
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        class _BlockingRefreshDB:
            async def execute(self, statement):
                refresh_started.set()
                await release_refresh.wait()
                return _RowsResult([older_row])

        refresh_task = asyncio.create_task(
            system_settings.refresh_runtime_system_settings_from_db(
                _BlockingRefreshDB()
            )
        )
        await asyncio.wait_for(refresh_started.wait(), timeout=0.1)
        newer_url = "https://oapi.dingtalk.com/robot/send?access_token=new-test-token"
        body = {
            "webhook_url": newer_url,
            "enabled": True,
            "availability_threshold": 5,
            "authentication_threshold": 3,
            "window_seconds": 60,
            "dedup_seconds": 300,
            "max_pending_tasks": 64,
        }
        patch_db = _FakeDB()
        patch_committed = asyncio.Event()

        async def mark_patch_committed():
            patch_committed.set()

        patch_db.commit.side_effect = mark_patch_committed
        self._override_db(patch_db)

        async def patch_config():
            async with await self._client() as client:
                return await client.patch("/admin/alerts/config", json=body)

        patch_task = asyncio.create_task(patch_config())
        await asyncio.wait_for(patch_committed.wait(), timeout=0.1)
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(patch_task), timeout=0.01)
        finally:
            release_refresh.set()
        response, _ = await asyncio.gather(patch_task, refresh_task)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(alert_admin.current_alert_webhook_url(), newer_url)
        self.assertEqual(
            model_registry.current_system_settings()["fallback_alert_webhook_url"],
            newer_url,
        )

    async def test_runtime_refresh_accepts_changed_content_at_same_version(self) -> None:
        updated_at = datetime(2026, 7, 21, 12, 0, 2)
        first = SimpleNamespace(
            setting_key="fallback_alert_webhook_url",
            setting_value="https://first.example/webhook",
            updated_at=updated_at,
        )
        second = SimpleNamespace(
            setting_key="fallback_alert_webhook_url",
            setting_value="https://second.example/webhook",
            updated_at=updated_at,
        )

        await system_settings.refresh_runtime_system_settings_from_db(
            _FakeDB(execute_results=[_RowsResult([first])])
        )
        await system_settings.refresh_runtime_system_settings_from_db(
            _FakeDB(execute_results=[_RowsResult([second])])
        )

        self.assertEqual(
            alert_admin.current_alert_webhook_url(),
            "https://second.example/webhook",
        )
        self.assertEqual(
            model_registry.current_system_settings()["fallback_alert_webhook_url"],
            alert_admin.current_alert_webhook_url(),
        )

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
