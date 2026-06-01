import unittest
from datetime import datetime
from types import SimpleNamespace

import httpx

from app.channel_monitoring import (
    monitor_model_list,
    run_provider_channel_monitor_once,
    serialize_monitor_models,
)
from app.models import (
    ProviderChannel,
    ProviderChannelMonitor,
    ProviderChannelMonitorDailyRollup,
    ProviderChannelMonitorHistory,
)
from app.security import encrypt_api_key


class _FakeScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, *, monitor, channel, daily_rows=None):
        self.monitor = monitor
        self.channel = channel
        self.daily_rows = list(daily_rows or [])
        self.added = []
        self.commits = 0
        self.queries = []

    async def get(self, model, key):
        if model is ProviderChannelMonitor and key == self.monitor.id:
            return self.monitor
        if model is ProviderChannel and key == self.channel.id:
            return self.channel
        return None

    async def execute(self, query):
        self.queries.append(query)
        row = self.daily_rows.pop(0) if self.daily_rows else None
        return _FakeScalarOneResult(row)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, headers):
        self.calls.append(("GET", url, dict(headers), None))
        return httpx.Response(200, json={"data": [{"id": "gpt-5.3-codex"}]})

    async def post(self, url, json, headers):
        self.calls.append(("POST", url, dict(headers), dict(json)))
        return httpx.Response(
            200,
            json={
                "id": "resp_monitor",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
            },
        )


class ChannelMonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_monitor_once_records_history_and_daily_rollup(self) -> None:
        monitor = SimpleNamespace(
            id="cmon_test",
            channel_id="ch_test",
            name="North Star gpt-5.3",
            endpoint="responses",
            primary_model="gpt-5.3-codex",
            extra_models=serialize_monitor_models(["gpt-5.4"]),
            status="active",
            interval_seconds=60,
            timeout_seconds=30,
            last_checked_at=None,
            last_status="",
            last_latency_ms=0,
            last_ping_latency_ms=0,
            last_message="",
        )
        channel = SimpleNamespace(
            id="ch_test",
            base_url="https://sub2api.example",
            encrypted_api_key=encrypt_api_key("sk-test"),
            auth_style="bearer",
        )
        db = _FakeDB(monitor=monitor, channel=channel)
        client = _FakeClient()

        results = await run_provider_channel_monitor_once(db, monitor.id, client=client)

        self.assertEqual([item.model for item in results], ["gpt-5.3-codex", "gpt-5.4"])
        self.assertEqual([item.status for item in results], ["operational", "operational"])
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.calls[0][1], "https://sub2api.example/v1/models")
        self.assertEqual(client.calls[0][2]["authorization"], "Bearer sk-test")
        self.assertEqual(client.calls[1][0], "POST")
        self.assertEqual(client.calls[1][1], "https://sub2api.example/v1/responses")
        self.assertEqual(client.calls[1][3]["model"], "gpt-5.3-codex")
        self.assertEqual(client.calls[2][3]["model"], "gpt-5.4")
        self.assertEqual(db.commits, 1)
        self.assertEqual(monitor_model_list(monitor), ["gpt-5.3-codex", "gpt-5.4"])
        self.assertEqual(monitor.last_status, "operational")
        self.assertEqual(monitor.last_message, "ok")
        self.assertIsInstance(monitor.last_checked_at, datetime)
        histories = [obj for obj in db.added if isinstance(obj, ProviderChannelMonitorHistory)]
        daily_rows = [obj for obj in db.added if isinstance(obj, ProviderChannelMonitorDailyRollup)]
        self.assertEqual(len(histories), 2)
        self.assertEqual(len(daily_rows), 2)
        self.assertEqual(daily_rows[0].total_checks, 1)
        self.assertEqual(daily_rows[0].operational_count, 1)
