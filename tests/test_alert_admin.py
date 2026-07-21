import asyncio
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://test@127.0.0.1:3306/test")

import app.alert_admin as alert_admin
import app.db as db_module
import app.main as main_module
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
        self.queries = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def get(self, model, key):
        return self.items.get(key)

    def add(self, item):
        self.added.append(item)
        self.items[item.setting_key] = item

    async def execute(self, statement):
        self.queries.append(statement)
        return self.execute_results.pop(0) if self.execute_results else _RowsResult([])


class _ObservedLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.attempts = 0
        self.second_attempted = asyncio.Event()

    async def __aenter__(self):
        self.attempts += 1
        if self.attempts == 2:
            self.second_attempted.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self._lock.release()


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
        settings.fallback_alert_webhook_url = (
            "https://oapi.dingtalk.com/robot/send?access_token=top-secret"
        )
        system_settings._RUNTIME_SYSTEM_SETTINGS_LOCK = asyncio.Lock()
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

    async def _assert_concurrent_patch_last_commit_wins(self, final_webhook_url: str):
        canonical_db = {"webhook_url": None}
        first_committed = asyncio.Event()
        release_first_commit = asyncio.Event()

        class _CommitOrderDB(_FakeDB):
            def __init__(self, webhook_url, *, delay_commit_return=False):
                super().__init__()
                self.webhook_url = webhook_url
                self.delay_commit_return = delay_commit_return
                self.commit = AsyncMock(side_effect=self._commit)

            async def _commit(self):
                canonical_db["webhook_url"] = self.webhook_url
                if self.delay_commit_return:
                    first_committed.set()
                    await release_first_commit.wait()

        first_url = "https://oapi.dingtalk.com/robot/send?access_token=first-test-token"
        databases = iter(
            (
                _CommitOrderDB(first_url, delay_commit_return=True),
                _CommitOrderDB(final_webhook_url),
            )
        )

        async def fake_get_db():
            yield next(databases)

        app.dependency_overrides[alert_admin.get_db] = fake_get_db

        def body(webhook_url):
            return {
                "webhook_url": webhook_url,
                "enabled": True,
                "availability_threshold": 5,
                "authentication_threshold": 3,
                "window_seconds": 60,
                "dedup_seconds": 300,
                "max_pending_tasks": 64,
            }

        async def patch_config(webhook_url):
            async with await self._client() as client:
                return await client.patch(
                    "/admin/alerts/config",
                    json=body(webhook_url),
                )

        observed_lock = _ObservedLock()
        system_settings._RUNTIME_SYSTEM_SETTINGS_LOCK = observed_lock
        first_task = asyncio.create_task(patch_config(first_url))
        await asyncio.wait_for(first_committed.wait(), timeout=0.1)
        second_task = asyncio.create_task(patch_config(final_webhook_url))
        try:
            await asyncio.wait_for(observed_lock.second_attempted.wait(), timeout=0.1)
            self.assertFalse(second_task.done())
        finally:
            release_first_commit.set()
        first_response, second_response = await asyncio.gather(first_task, second_task)

        self.assertEqual(first_response.status_code, 200, first_response.text)
        self.assertEqual(second_response.status_code, 200, second_response.text)
        self.assertEqual(canonical_db["webhook_url"], final_webhook_url)
        self.assertEqual(alert_admin.current_alert_webhook_url(), final_webhook_url)
        self.assertEqual(
            model_registry.current_system_settings()["fallback_alert_webhook_url"],
            final_webhook_url,
        )

    async def test_config_requires_admin_token(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/admin/alerts/config")

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_unauthenticated_patch_does_not_execute_database_work(self) -> None:
        fake_db = _FakeDB()
        self._override_db(fake_db)
        body = {
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=must-not-save",
            "enabled": True,
            "availability_threshold": 5,
            "authentication_threshold": 3,
            "window_seconds": 60,
            "dedup_seconds": 300,
            "max_pending_tasks": 64,
        }

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.patch("/admin/alerts/config", json=body)

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(fake_db.queries, [])
        fake_db.commit.assert_not_awaited()

    async def test_config_body_validation_errors_are_sanitized_and_not_cached(self) -> None:
        cases = (
            {},
            {
                "content": b'{"webhook_url":"must-not-echo-malformed"',
                "headers": {
                    "content-type": "application/json",
                    "origin": "https://admin.example",
                },
            },
            {
                "json": ["must-not-echo-non-object"],
                "headers": {"origin": "https://admin.example"},
            },
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
                    if "headers" in request_kwargs:
                        self.assertEqual(
                            response.headers["access-control-allow-origin"],
                            "*",
                        )

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
            "https://oapi.dingtalk.com/robot/send?access_token=top-secret",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(payload["last_success_at"], "2026-07-21T10:00:00")
        self.assertEqual(payload["last_failure_at"], "2026-07-21T11:00:00")

    async def test_malformed_stored_webhook_remains_visible_but_is_not_configured(self) -> None:
        malformed_url = "https://internal.example/robot/send?access_token=legacy-token"
        system_settings._apply_runtime_system_settings(
            {"fallback_alert_webhook_url": malformed_url},
            replace=True,
        )
        fake_db = _FakeDB(execute_results=[_RowsResult([])])
        self._override_db(fake_db)

        async with await self._client() as client:
            response = await client.get("/admin/alerts/config")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["webhook_url"], malformed_url)
        self.assertFalse(response.json()["webhook_configured"])
        self.assertEqual(response.headers["cache-control"], "no-store")

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

    async def test_patch_database_failures_are_sanitized_and_rolled_back(self) -> None:
        body = {
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=synthetic-secret-token",
            "enabled": True,
            "availability_threshold": 8,
            "authentication_threshold": 4,
            "window_seconds": 90,
            "dedup_seconds": 600,
            "max_pending_tasks": 64,
        }
        for operation in ("execute", "commit"):
            with self.subTest(operation=operation):
                fake_db = _FakeDB()
                failure = RuntimeError(
                    f"SQL params include {body['webhook_url']}"
                )
                if operation == "execute":
                    fake_db.execute = AsyncMock(side_effect=failure)
                    fake_db.rollback.side_effect = RuntimeError(
                        f"rollback also included {body['webhook_url']}"
                    )
                else:
                    fake_db.commit.side_effect = failure
                self._override_db(fake_db)

                async with await self._client(raise_app_exceptions=False) as client:
                    response = await client.patch("/admin/alerts/config", json=body)

                self.assertEqual(response.status_code, 500, response.text)
                self.assertEqual(
                    response.headers.get("content-type"),
                    "application/json",
                )
                self.assertEqual(
                    response.json(),
                    {"detail": "Unable to save alert configuration"},
                )
                self.assertNotIn("synthetic-secret-token", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                fake_db.rollback.assert_awaited_once()

    def test_database_engine_hides_bound_parameters(self) -> None:
        self.assertTrue(db_module.engine.sync_engine.hide_parameters)

    async def test_persist_failure_raises_context_free_sanitized_error(self) -> None:
        secret = "synthetic-persist-secret"
        fake_db = _FakeDB()
        fake_db.execute = AsyncMock(
            side_effect=RuntimeError(f"database parameters contained {secret}")
        )

        with self.assertRaises(
            system_settings.RuntimeSystemSettingsPersistenceError
        ) as caught:
            await system_settings.persist_runtime_system_settings(
                fake_db,
                {"fallback_alert_webhook_url": secret},
            )

        self.assertEqual(caught.exception.args, ())
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(secret, str(caught.exception))
        fake_db.rollback.assert_awaited_once()

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
            "\x00https://oapi.dingtalk.com/robot/send?access_token=must-not-echo",
            "https://oapi.ding\ntalk.com/robot/send?access_token=must-not-echo",
            "https://oapi.dingtalk.com/robot/send?access_token=must%0Anot-echo",
            "https://oapi.dingtalk.com/robot/send?access_token=must%20not-echo",
            "https://oapi.dingtalk.com/robot/send?access_token=must-not-echo\x7f",
            "https://oapi.dingtalk.com/robot/send?access_token=must%00not-echo",
            "https://oapi.dingtalk.com/robot/send?access_token=must%01not-echo",
            "https://oapi.dingtalk.com/robot/send?access_token=must%1Bnot-echo",
            "https://oapi.dingtalk.com/robot/send?access_token=must%7Fnot-echo",
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
                    response = await client.patch(
                        "/admin/alerts/config",
                        json=body,
                        headers={"origin": "https://admin.example"},
                    )

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    response.json(),
                    {"detail": "invalid alert config"},
                )
                self.assertNotIn("must-not-echo", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["access-control-allow-origin"], "*")
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
            response = await client.patch(
                "/admin/alerts/config",
                json=body,
                headers={"origin": "https://admin.example"},
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json(), {"detail": "invalid alert config"})
        self.assertNotIn("must-not-echo-parser", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        fake_db.commit.assert_not_awaited()

    async def test_concurrent_patch_last_nonempty_commit_wins(self) -> None:
        await self._assert_concurrent_patch_last_commit_wins(
            "https://oapi.dingtalk.com/robot/send?access_token=second-test-token"
        )

    async def test_concurrent_patch_last_empty_commit_disables_webhook(self) -> None:
        await self._assert_concurrent_patch_last_commit_wins("")

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

    async def test_runtime_refresh_without_database_row_restores_environment_fallback(self) -> None:
        environment_url = (
            "https://oapi.dingtalk.com/robot/send?access_token=environment-token"
        )
        settings.fallback_alert_webhook_url = environment_url
        system_settings._apply_runtime_system_settings(
            {
                "fallback_alert_webhook_url":
                    "https://oapi.dingtalk.com/robot/send?access_token=database-token"
            },
            replace=True,
        )

        await system_settings.refresh_runtime_system_settings_from_db(
            _FakeDB(execute_results=[_RowsResult([])])
        )

        self.assertEqual(alert_admin.current_alert_webhook_url(), environment_url)

    async def test_runtime_poll_repairs_local_aba_change_when_database_state_is_unchanged(self) -> None:
        database_url = "https://oapi.dingtalk.com/robot/send?access_token=database-b"
        local_url = "https://oapi.dingtalk.com/robot/send?access_token=local-a"
        database_state = (("fallback_alert_webhook_url", database_url),)
        refresh_calls = 0
        sleep_calls = 0

        class _SessionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        async def refresh_to_database(_db):
            nonlocal refresh_calls
            refresh_calls += 1
            system_settings._apply_runtime_system_settings(
                dict(database_state),
                replace=True,
            )

        async def advance_loop(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                system_settings._apply_runtime_system_settings(
                    {"fallback_alert_webhook_url": local_url},
                    replace=True,
                )
                return
            raise RuntimeError("stop deterministic refresh loop")

        system_settings._apply_runtime_system_settings(
            {"fallback_alert_webhook_url": local_url},
            replace=True,
        )
        with (
            patch.object(db_module, "SessionLocal", side_effect=lambda: _SessionContext()),
            patch.object(
                main_module,
                "get_runtime_system_settings_db_state",
                AsyncMock(return_value=database_state),
            ),
            patch.object(
                main_module,
                "refresh_runtime_system_settings_from_db",
                side_effect=refresh_to_database,
            ),
            patch.object(main_module.asyncio, "sleep", side_effect=advance_loop),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop deterministic refresh loop"):
                await main_module.runtime_system_settings_refresh_loop(1)

        self.assertEqual(refresh_calls, 2)
        self.assertEqual(alert_admin.current_alert_webhook_url(), database_url)

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

        observed_lock = _ObservedLock()
        system_settings._RUNTIME_SYSTEM_SETTINGS_LOCK = observed_lock
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
        try:
            await asyncio.wait_for(observed_lock.second_attempted.wait(), timeout=0.1)
            self.assertFalse(patch_committed.is_set())
        finally:
            release_refresh.set()
        response, _ = await asyncio.gather(patch_task, refresh_task)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(patch_committed.is_set())
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
