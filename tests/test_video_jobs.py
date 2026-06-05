import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.main import app
from app.models import RequestLog, UsageDaily, VideoJob
from app.router import registry
import app.video_jobs as video_jobs_module


class _FakeExecuteResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self


class _VideoJobStore:
    def __init__(self, user) -> None:
        self.jobs = {}
        self.user = user
        self.request_logs = []
        self.ledger_entries = []
        self.other_added = []


def _criterion_name_value(criterion) -> tuple[str | None, object | None]:
    left = getattr(criterion, "left", None)
    right = getattr(criterion, "right", None)
    return getattr(left, "name", None), getattr(right, "value", None)


def _statement_filters(statement) -> tuple[dict, list[list[tuple[str, object]]]]:
    filters = {}
    or_groups = []
    for criterion in getattr(statement, "_where_criteria", ()):
        clauses = list(getattr(criterion, "clauses", ()) or ())
        if clauses:
            or_groups.append(
                [
                    (name, value)
                    for name, value in (_criterion_name_value(item) for item in clauses)
                    if name is not None
                ]
            )
            continue
        key, value = _criterion_name_value(criterion)
        if key is not None:
            filters[key] = value
    return filters, or_groups


def _matches_filters(row, filters: dict, or_groups: list[list[tuple[str, object]]]) -> bool:
    for key, value in filters.items():
        if getattr(row, key, None) != value:
            return False
    for group in or_groups:
        if not any(getattr(row, key, None) == value for key, value in group):
            return False
    return True


class _FakeVideoDBSession:
    def __init__(self, store: _VideoJobStore) -> None:
        self.store = store

    def add(self, item) -> None:
        if isinstance(item, VideoJob):
            if not item.created_at:
                item.created_at = datetime.utcnow()
            self.store.jobs[item.id] = item
            return
        if isinstance(item, RequestLog):
            self.store.request_logs.append(item)
            return
        if isinstance(item, UsageDaily):
            self.store.other_added.append(item)
            return
        if item.__class__.__name__ == "BillingLedgerEntry":
            self.store.ledger_entries.append(item)
            return
        self.store.other_added.append(item)

    async def commit(self) -> None:
        return None

    async def refresh(self, item) -> None:
        if isinstance(item, VideoJob) and not item.created_at:
            item.created_at = datetime.utcnow()

    async def execute(self, statement):
        statement_text = str(statement)
        filters, or_groups = _statement_filters(statement)
        if "coincoin_users" in statement_text:
            if filters.get("id") == self.store.user.id:
                return _FakeExecuteResult(scalar=self.store.user)
            return _FakeExecuteResult(scalar=None)
        if "coincoin_video_jobs" in statement_text:
            for job in self.store.jobs.values():
                if _matches_filters(job, filters, or_groups):
                    return _FakeExecuteResult(scalar=job)
        return _FakeExecuteResult(scalar=None)


class _FakeSeedanceResponse:
    def __init__(self, payload, status_code: int = 200, headers: dict | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _RecordingSeedanceClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected Seedance upstream call")
        return self.responses.pop(0)


class VideoJobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._originals = {
            "model_catalog_json": settings.model_catalog_json,
            "model_alias_overrides_path": settings.model_alias_overrides_path,
            "billing_mode": settings.billing_mode,
        }
        settings.model_alias_overrides_path = ""
        settings.billing_mode = "none"
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "gpt-5.4",
                "default_video_model": "seedance-v2-720p",
                "models": [
                    {
                        "id": "gpt-5.4",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "capabilities": ["chat/completions", "responses"],
                        "routing_mode": "legacy_auto",
                        "delivery_lane": "legacy",
                    },
                    {
                        "id": "seedance-v2-720p",
                        "owned_by": "bytedance",
                        "provider_name": "Seedance",
                        "provider_model": "seedance-v2-720p",
                        "capabilities": ["videos/generations"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "seedance-v2-720p",
                        "upstream_url": "https://api.wgspai.cn",
                        "api_key": "seedance-key",
                        "auth_style": "bearer",
                        "price_per_video_cents": 98,
                        "billable_sku": "seedance-v2-720p-video-task",
                    },
                    {
                        "id": "seedance-v2-720p-video",
                        "owned_by": "bytedance",
                        "provider_name": "Seedance",
                        "provider_model": "seedance-v2-720p-video",
                        "capabilities": ["videos/generations"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "seedance-v2-720p-video",
                        "upstream_url": "https://api.wgspai.cn",
                        "api_key": "seedance-key",
                        "auth_style": "bearer",
                        "price_per_video_cents": 112,
                        "billable_sku": "seedance-v2-720p-video-reference-task",
                    },
                ],
            }
        )
        registry._initialized = False
        registry.init_from_settings()

        self.fake_user = SimpleNamespace(id="u_video", _api_key_id="k_video_user", balance=1000)
        self.store = _VideoJobStore(self.fake_user)

        async def fake_db():
            yield _FakeVideoDBSession(self.store)

        app.dependency_overrides[video_jobs_module.get_db] = fake_db

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry.clear_runtime_alias_overrides()
        registry.clear_runtime_pricing_overrides()
        registry._initialized = False
        app.dependency_overrides.pop(video_jobs_module.get_db, None)

    async def test_rejects_pure_text_seedance_request(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authorize_request", AsyncMock(return_value=self.fake_user)):
                response = await client.post(
                    "/v1/videos/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "seedance-v2-720p",
                        "prompt": "A cinematic beach shot",
                        "params": {"ratio": "16:9"},
                    },
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "missing_reference_media")
        self.assertEqual(self.store.jobs, {})

    async def test_rejects_video_reference_on_non_video_seedance_model(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authorize_request", AsyncMock(return_value=self.fake_user)):
                response = await client.post(
                    "/v1/videos/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "seedance-v2-720p",
                        "prompt": "Match the camera movement",
                        "params": {
                            "ratio": "16:9",
                            "content": [
                                {
                                    "type": "video_url",
                                    "video_url": {"url": "https://example.com/motion.mp4"},
                                    "role": "reference_video",
                                }
                            ],
                        },
                    },
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "video_reference_requires_video_model")
        self.assertEqual(self.store.jobs, {})

    async def test_create_video_generation_posts_seedance_task_and_records_usage(self) -> None:
        settings.billing_mode = "balance"
        upstream_client = _RecordingSeedanceClient(
            [
                _FakeSeedanceResponse(
                    {
                        "code": 0,
                        "data": {
                            "task_id": "task_seedance_1",
                            "model": "seedance-v2-720p",
                            "status": "pending",
                        },
                    },
                    headers={"x-request-id": "req_seedance_create_1"},
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                video_jobs_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(video_jobs_module.usage_buffer, "get_pending_cost", AsyncMock(return_value=0)), patch.object(
                video_jobs_module.usage_buffer,
                "add",
                AsyncMock(),
            ) as add_usage:
                response = await client.post(
                    "/v1/videos/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "seedance-v2-720p",
                        "prompt": "Camera slowly pushes in",
                        "params": {
                            "ratio": "16:9",
                            "images": ["https://example.com/ref.jpg"],
                        },
                    },
                )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], video_jobs_module.JOB_STATUS_QUEUED)
        self.assertEqual(payload["model"], "seedance-v2-720p")
        self.assertEqual(payload["upstream_task_id"], "task_seedance_1")
        self.assertEqual(payload["charged_cents"], 98)
        self.assertEqual(len(self.store.jobs), 1)

        job = next(iter(self.store.jobs.values()))
        self.assertEqual(job.api_key_id, "k_video_user")
        self.assertEqual(job.upstream_request_id, "req_seedance_create_1")
        self.assertEqual(job.provider_model, "seedance-v2-720p")
        self.assertEqual(job.legacy_debit_cents, 98)
        self.assertEqual(job.subscription_debit_cents, 0)
        self.assertEqual(job.traffic_pack_debit_cents, 0)
        self.assertEqual(self.fake_user.balance, 902)
        self.assertEqual(upstream_client.calls[0]["url"], "https://api.wgspai.cn/v1/task/create")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer seedance-key")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "seedance-v2-720p")
        self.assertEqual(upstream_client.calls[0]["json"]["params"]["ratio"], "16:9")

        add_usage.assert_not_awaited()
        self.assertEqual(len(self.store.request_logs), 1)
        self.assertEqual(self.store.request_logs[0].endpoint, "videos/generations")
        self.assertEqual(self.store.request_logs[0].usage_unit_type, "videos")
        self.assertEqual(self.store.request_logs[0].usage_unit_count, 1)
        self.assertEqual(self.store.request_logs[0].video_count, 1)
        self.assertEqual(self.store.request_logs[0].price_per_video_cents, 98)
        self.assertEqual(self.store.request_logs[0].cost_cents, 98)
        self.assertEqual(len(self.store.ledger_entries), 1)
        self.assertEqual(self.store.ledger_entries[0].entry_type, "usage_legacy_balance_debit")
        self.assertEqual(self.store.ledger_entries[0].amount_cents, -98)
        self.assertEqual(self.store.ledger_entries[0].source_type, "video_job")
        self.assertEqual(self.store.ledger_entries[0].source_id, job.id)

    async def test_create_video_generation_without_model_uses_default_video_model(self) -> None:
        upstream_client = _RecordingSeedanceClient(
            [
                _FakeSeedanceResponse(
                    {"code": 0, "data": {"task_id": "task_default_video", "status": "pending"}}
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authorize_request", AsyncMock(return_value=self.fake_user)), patch.object(
                video_jobs_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(video_jobs_module.usage_buffer, "add", AsyncMock()):
                response = await client.post(
                    "/v1/videos/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "prompt": "Use the image as a first frame",
                        "params": {
                            "ratio": "9:16",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "https://example.com/start.png"},
                                    "role": "first_frame",
                                }
                            ],
                        },
                    },
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["model"], "seedance-v2-720p")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "seedance-v2-720p")

    async def test_get_video_generation_queries_upstream_and_returns_output_url(self) -> None:
        job = VideoJob(
            id="job_video_get_1",
            user_id=self.fake_user.id,
            api_key_id="k_video_user",
            status=video_jobs_module.JOB_STATUS_RUNNING,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            route_reason="catalog:seedance-v2-720p:upstream_direct",
            upstream_task_id="task_seedance_done",
            request_payload_json="{}",
            charged_cents=98,
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingSeedanceClient(
            [
                _FakeSeedanceResponse(
                    {
                        "code": 0,
                        "data": {
                            "task_id": job.upstream_task_id,
                            "status": "completed",
                            "output": {"url": "https://example.com/generated.mp4"},
                        },
                    },
                    headers={"x-request-id": "req_seedance_query_1"},
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authenticate_user", AsyncMock(return_value=self.fake_user)), patch.object(
                video_jobs_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ):
                response = await client.get(
                    f"/v1/videos/generations/{job.id}",
                    headers={"Authorization": "Bearer sk_cc_test"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], video_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(payload["output"]["url"], "https://example.com/generated.mp4")
        self.assertEqual(self.store.jobs[job.id].attempt_count, 1)
        self.assertEqual(self.store.jobs[job.id].upstream_request_id, "req_seedance_query_1")
        self.assertEqual(upstream_client.calls[0]["url"], "https://api.wgspai.cn/v1/task/query")
        self.assertEqual(upstream_client.calls[0]["json"], {"task_id": job.upstream_task_id})

    async def test_failed_video_generation_query_refunds_once(self) -> None:
        self.fake_user.balance = 902
        job = VideoJob(
            id="job_video_fail_1",
            user_id=self.fake_user.id,
            api_key_id="k_video_user",
            status=video_jobs_module.JOB_STATUS_RUNNING,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            route_reason="catalog:seedance-v2-720p:upstream_direct",
            upstream_task_id="task_seedance_failed",
            request_payload_json="{}",
            charged_cents=98,
            legacy_debit_cents=98,
            refunded_cents=0,
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingSeedanceClient(
            [
                _FakeSeedanceResponse(
                    {
                        "code": 0,
                        "data": {
                            "task_id": job.upstream_task_id,
                            "status": "failed",
                            "fail_reason": "upstream failed",
                        },
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authenticate_user", AsyncMock(return_value=self.fake_user)), patch.object(
                video_jobs_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(video_jobs_module, "increment_finance_summary", AsyncMock()) as finance_summary:
                first = await client.get(
                    f"/v1/videos/generations/{job.id}",
                    headers={"Authorization": "Bearer sk_cc_test"},
                )
                second = await client.get(
                    f"/v1/videos/generations/{job.id}",
                    headers={"Authorization": "Bearer sk_cc_test"},
                )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["status"], video_jobs_module.JOB_STATUS_FAILED)
        self.assertEqual(second.json()["refunded_cents"], 98)
        self.assertEqual(self.fake_user.balance, 1000)
        self.assertEqual(self.store.jobs[job.id].refunded_cents, 98)
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(len(self.store.request_logs), 1)
        self.assertEqual(self.store.request_logs[0].cost_cents, -98)
        self.assertEqual(self.store.request_logs[0].usage_unit_type, "videos")
        self.assertEqual(self.store.request_logs[0].video_count, -1)
        self.assertEqual(len(self.store.ledger_entries), 1)
        self.assertEqual(self.store.ledger_entries[0].entry_type, "usage_legacy_balance_refund")
        self.assertEqual(self.store.ledger_entries[0].amount_cents, 98)
        finance_summary.assert_awaited_once()

    async def test_failed_video_generation_without_persisted_debit_does_not_refund(self) -> None:
        job = VideoJob(
            id="job_video_fail_without_debit",
            user_id=self.fake_user.id,
            api_key_id="k_video_user",
            status=video_jobs_module.JOB_STATUS_RUNNING,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            route_reason="catalog:seedance-v2-720p:upstream_direct",
            upstream_task_id="task_seedance_failed_without_debit",
            request_payload_json="{}",
            charged_cents=98,
            refunded_cents=0,
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingSeedanceClient(
            [
                _FakeSeedanceResponse(
                    {
                        "code": 0,
                        "data": {
                            "task_id": job.upstream_task_id,
                            "status": "failed",
                            "fail_reason": "upstream failed",
                        },
                    }
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authenticate_user", AsyncMock(return_value=self.fake_user)), patch.object(
                video_jobs_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(video_jobs_module, "increment_finance_summary", AsyncMock()) as finance_summary:
                response = await client.get(
                    f"/v1/videos/generations/{job.id}",
                    headers={"Authorization": "Bearer sk_cc_test"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], video_jobs_module.JOB_STATUS_FAILED)
        self.assertEqual(response.json()["refunded_cents"], 0)
        self.assertEqual(self.fake_user.balance, 1000)
        self.assertEqual(self.store.jobs[job.id].refunded_cents, 0)
        self.assertEqual(len(self.store.request_logs), 0)
        self.assertEqual(len(self.store.ledger_entries), 0)
        finance_summary.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
