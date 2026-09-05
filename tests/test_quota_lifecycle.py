import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.quota_client import QuotaDecision
from app.quota_lifecycle import (
    QuotaReservationASGIMiddleware,
    clear_current_quota_reservation,
    current_quota_reservation_id,
    reserve_quota_for_request,
)
from app.usage_buffer import schedule_usage_add


class _FakeQuotaClient:
    def __init__(self, reserve_decision=None):
        self.reserve_decision = reserve_decision or QuotaDecision(
            allowed=True,
            reservation_id="qres_fake",
            reason="reserved",
        )
        self.reserves = []
        self.commits = []
        self.releases = []

    async def reserve(self, **payload):
        self.reserves.append(payload)
        return self.reserve_decision

    async def commit(self, reservation_id, actual_cost_cents=0):
        self.commits.append((reservation_id, actual_cost_cents))
        return QuotaDecision(allowed=True, reservation_id=reservation_id, reason="committed")

    async def release(self, reservation_id):
        self.releases.append(reservation_id)
        return QuotaDecision(allowed=True, reservation_id=reservation_id, reason="released")


class QuotaLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._enabled = settings.quota_reservation_enabled
        self._url = settings.quota_service_url
        self._ttl = settings.quota_reservation_ttl_seconds
        self._user_limit = settings.quota_user_concurrency_limit
        self._key_limit = settings.quota_api_key_concurrency_limit
        self._station_limit = settings.quota_station_concurrency_limit
        settings.quota_reservation_enabled = True
        settings.quota_service_url = "http://quota.local"
        settings.quota_reservation_ttl_seconds = 45
        settings.quota_user_concurrency_limit = 2
        settings.quota_api_key_concurrency_limit = 3
        settings.quota_station_concurrency_limit = 4
        clear_current_quota_reservation()

    async def asyncTearDown(self):
        clear_current_quota_reservation()
        settings.quota_reservation_enabled = self._enabled
        settings.quota_service_url = self._url
        settings.quota_reservation_ttl_seconds = self._ttl
        settings.quota_user_concurrency_limit = self._user_limit
        settings.quota_api_key_concurrency_limit = self._key_limit
        settings.quota_station_concurrency_limit = self._station_limit

    @staticmethod
    def _request() -> Request:
        return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})

    async def test_reserve_builds_distributed_limits_and_context(self):
        fake = _FakeQuotaClient()
        user = SimpleNamespace(
            id="u_1",
            request_limit_per_minute=60,
            _api_key_id="key_1",
            _station_context={"station_id": "st_1"},
        )

        with patch("app.quota_lifecycle.quota_reservation_client", fake):
            await reserve_quota_for_request(self._request(), user, available_balance_cents=500, estimated_cost_cents=25)

        self.assertEqual(current_quota_reservation_id(), "qres_fake")
        payload = fake.reserves[0]
        self.assertEqual(payload["user_id"], "u_1")
        self.assertEqual(payload["api_key_id"], "key_1")
        self.assertEqual(payload["station_id"], "st_1")
        self.assertEqual(payload["estimated_cost_cents"], 25)
        self.assertEqual(payload["available_balance_cents"], 500)
        self.assertEqual(payload["ttl_seconds"], 45)
        self.assertEqual(payload["rpm_limits"][0]["limit"], 60)
        self.assertIn({"dimension": "user", "id": "u_1", "limit": 2}, payload["concurrency_limits"])
        self.assertIn({"dimension": "api_key", "id": "key_1", "limit": 3}, payload["concurrency_limits"])
        self.assertIn({"dimension": "station", "id": "st_1", "limit": 4}, payload["concurrency_limits"])

    async def test_reserve_denial_maps_to_http_error(self):
        fake = _FakeQuotaClient(
            QuotaDecision(allowed=False, reservation_id="qres_denied", reason="concurrency_exceeded:user")
        )
        user = SimpleNamespace(id="u_1", request_limit_per_minute=None, _api_key_id="", _station_context={})

        with patch("app.quota_lifecycle.quota_reservation_client", fake):
            with self.assertRaises(HTTPException) as ctx:
                await reserve_quota_for_request(self._request(), user, available_balance_cents=0, estimated_cost_cents=1)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail, "concurrency limit exceeded")

    async def test_middleware_waits_for_usage_task_and_commits_instead_of_release(self):
        fake = _FakeQuotaClient()

        async def app(_scope, _receive, send):
            await reserve_quota_for_request(
                self._request(),
                SimpleNamespace(id="u_1", request_limit_per_minute=None, _api_key_id="", _station_context={}),
                available_balance_cents=100,
                estimated_cost_cents=1,
            )
            schedule_usage_add(
                "u_1",
                input_tokens=10,
                output_tokens=5,
                requests=1,
                price_input_per_million=100000,
                price_output_per_million=100000,
            )
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        with patch("app.quota_lifecycle.quota_reservation_client", fake), patch(
            "app.usage_events.schedule_usage_event_shadow"
        ):
            await QuotaReservationASGIMiddleware(app)(
                {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
                receive,
                send,
            )

        self.assertEqual(sent[-1]["body"], b"ok")
        self.assertEqual(fake.commits, [("qres_fake", 2)])
        self.assertEqual(fake.releases, [])

    async def test_middleware_releases_when_no_usage_is_recorded(self):
        fake = _FakeQuotaClient()

        async def app(_scope, _receive, send):
            await reserve_quota_for_request(
                self._request(),
                SimpleNamespace(id="u_1", request_limit_per_minute=None, _api_key_id="", _station_context={}),
                available_balance_cents=100,
                estimated_cost_cents=1,
            )
            await send({"type": "http.response.start", "status": 502, "headers": []})
            await send({"type": "http.response.body", "body": b"bad", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        with patch("app.quota_lifecycle.quota_reservation_client", fake):
            await QuotaReservationASGIMiddleware(app)(
                {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
                receive,
                AsyncMock(),
            )

        self.assertEqual(fake.commits, [])
        self.assertEqual(fake.releases, ["qres_fake"])


if __name__ == "__main__":
    unittest.main()
