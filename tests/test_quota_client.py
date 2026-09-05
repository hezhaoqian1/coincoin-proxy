import unittest
from unittest.mock import patch

import httpx

from app.config import settings
from app.quota_client import QuotaReservationClient


class _FakeAsyncClient:
    def __init__(self, response=None, error=None, **_kwargs):
        self.response = response
        self.error = error
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, json):
        self.requests.append((url, json))
        if self.error:
            raise self.error
        return self.response


class QuotaReservationClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._enabled = settings.quota_reservation_enabled
        self._url = settings.quota_service_url
        self._fail_open = settings.quota_service_fail_open
        settings.quota_reservation_enabled = True
        settings.quota_service_url = "http://quota.local"
        settings.quota_service_fail_open = True

    async def asyncTearDown(self):
        settings.quota_reservation_enabled = self._enabled
        settings.quota_service_url = self._url
        settings.quota_service_fail_open = self._fail_open

    async def test_reserve_posts_contract_and_parses_decision(self):
        response = httpx.Response(200, json={"allowed": True, "reservation_id": "qres_1", "reason": "reserved"})
        fake = _FakeAsyncClient(response=response)

        with patch("app.quota_client.httpx.AsyncClient", return_value=fake):
            decision = await QuotaReservationClient().reserve(
                user_id="u_1",
                estimated_cost_cents=10,
                available_balance_cents=100,
                rpm_limits=[{"dimension": "user", "id": "u_1", "limit": 60}],
            )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reservation_id, "qres_1")
        self.assertEqual(fake.requests[0][0], "http://quota.local/v1/quota/reserve")
        self.assertEqual(fake.requests[0][1]["user_id"], "u_1")

    async def test_timeout_can_fail_open(self):
        fake = _FakeAsyncClient(error=httpx.ConnectTimeout("timeout"))

        with patch("app.quota_client.httpx.AsyncClient", return_value=fake):
            decision = await QuotaReservationClient().reserve(user_id="u_1")

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.fail_open)
        self.assertEqual(decision.reason, "quota_service_unavailable")

    async def test_timeout_can_fail_closed(self):
        settings.quota_service_fail_open = False
        fake = _FakeAsyncClient(error=httpx.ConnectTimeout("timeout"))

        with patch("app.quota_client.httpx.AsyncClient", return_value=fake):
            decision = await QuotaReservationClient().reserve(user_id="u_1")

        self.assertFalse(decision.allowed)
        self.assertFalse(decision.fail_open)

    async def test_disabled_client_allows_without_request(self):
        settings.quota_reservation_enabled = False
        decision = await QuotaReservationClient().reserve(user_id="u_1")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "disabled")


if __name__ == "__main__":
    unittest.main()
