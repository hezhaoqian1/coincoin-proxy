import unittest
import os
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("COINCOIN_DB_HOST", "localhost")
os.environ.setdefault("COINCOIN_DB_NAME", "test")
os.environ.setdefault("COINCOIN_DB_USER", "test")
os.environ.setdefault("COINCOIN_DB_PASSWORD", "test")

from app.main import app
import app.monitoring as monitoring_module


class MonitoringProbeTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        monitoring_module.settings.monitoring_token = ""
        monitoring_module.settings.monitoring_api_key = ""
        monitoring_module.settings.monitoring_public_base_url = ""
        monitoring_module.settings.monitoring_gateway_health_url = ""
        monitoring_module.settings.monitoring_chat_model = ""
        monitoring_module.settings.monitoring_responses_model = ""
        monitoring_module.settings.self_base_url = ""

    async def test_ops_summary_requires_monitoring_token(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/ops/monitoring/summary")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("monitoring token", response.text)

    async def test_ops_summary_accepts_monitoring_token(self) -> None:
        monitoring_module.settings.monitoring_token = "mon-secret"
        monitoring_module.settings.self_base_url = "https://proxy.example.com"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/ops/monitoring/summary",
                headers={"x-monitoring-token": "mon-secret"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["ui_scope"], "admin_only")
        self.assertFalse(payload["user_status_page"])
        self.assertIn("recommended_checks", payload["checkly"])

    async def test_admin_summary_requires_admin_token(self) -> None:
        monitoring_module.settings.admin_token = "admin-secret"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/admin/monitoring/summary")

        self.assertEqual(response.status_code, 401, response.text)

    async def test_public_health_probe_returns_upstream_status(self) -> None:
        monitoring_module.settings.monitoring_token = "mon-secret"
        monitoring_module.settings.monitoring_api_key = "sk-monitor"
        monitoring_module.settings.monitoring_public_base_url = (
            "https://proxy.example.com"
        )

        async def fake_request_json(method, url, headers=None, json_body=None):
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://proxy.example.com/health")
            return {
                "status_code": 200,
                "body": {"status": "ok", "service": "coincoin-proxy"},
                "latency_ms": 123,
                "headers": {},
            }

        with patch.object(
            monitoring_module, "_request_json", AsyncMock(side_effect=fake_request_json)
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/ops/monitoring/probes/public-health",
                    headers={"x-monitoring-token": "mon-secret"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["probe"], "public-health")
        self.assertEqual(payload["details"]["http_status"], 200)

    async def test_chat_probe_validates_marker(self) -> None:
        monitoring_module.settings.monitoring_token = "mon-secret"
        monitoring_module.settings.monitoring_api_key = "sk-monitor"
        monitoring_module.settings.monitoring_public_base_url = (
            "https://proxy.example.com"
        )
        monitoring_module.settings.monitoring_chat_model = "gpt-4o-mini"

        async def fake_request_json(method, url, headers=None, json_body=None):
            self.assertEqual(method, "POST")
            self.assertEqual(
                url, "https://proxy.example.com/v1/chat/completions"
            )
            self.assertEqual(json_body["model"], "gpt-4o-mini")
            return {
                "status_code": 200,
                "body": {
                    "choices": [
                        {
                            "message": {
                                "content": "COINCOIN_MONITOR_OK"
                            }
                        }
                    ]
                },
                "latency_ms": 456,
                "headers": {},
            }

        with patch.object(
            monitoring_module, "_request_json", AsyncMock(side_effect=fake_request_json)
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/ops/monitoring/probes/chat-completions",
                    headers={"x-monitoring-token": "mon-secret"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["probe"], "chat-completions")
        self.assertEqual(payload["details"]["model"], "gpt-4o-mini")

    async def test_gateway_probe_reports_missing_configuration(self) -> None:
        monitoring_module.settings.monitoring_token = "mon-secret"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/ops/monitoring/probes/gateway-readiness",
                headers={"x-monitoring-token": "mon-secret"},
            )

        self.assertEqual(response.status_code, 503, response.text)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["probe"], "gateway-readiness")
        self.assertIn("missing monitoring_gateway_health_url", payload["details"]["error"])


if __name__ == "__main__":
    unittest.main()
