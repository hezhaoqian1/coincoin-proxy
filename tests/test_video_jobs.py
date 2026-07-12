import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.main import app
from app.models import MediaArtifact, RequestLog, TrafficPackBalance, UsageDaily, UserSubscription, VideoJob
from app.router import registry
import app.video_jobs as video_jobs_module
from app import main as main_module


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
        self.media_artifacts = []
        self.ledger_entries = []
        self.other_added = []
        self.subscriptions = {}
        self.traffic_packs = {}


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
        self.queries = []

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
        if isinstance(item, MediaArtifact):
            self.store.media_artifacts.append(item)
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
        self.queries.append(statement)
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
        if "coincoin_user_subscriptions" in statement_text:
            for sub in self.store.subscriptions.values():
                if _matches_filters(sub, filters, or_groups):
                    return _FakeExecuteResult(scalar=sub)
        if "coincoin_traffic_pack_balances" in statement_text:
            for pack in self.store.traffic_packs.values():
                if _matches_filters(pack, filters, or_groups):
                    return _FakeExecuteResult(scalar=pack)
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
            with patch.object(video_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)):
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
            with patch.object(video_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)):
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
            with patch.object(video_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)), patch.object(
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

    async def test_create_video_generation_persists_wallet_allocation_payload(self) -> None:
        settings.billing_mode = "balance"
        upstream_client = _RecordingSeedanceClient(
            [
                _FakeSeedanceResponse(
                    {"code": 0, "data": {"task_id": "task_wallet_video", "status": "pending"}}
                )
            ]
        )
        allocation = {
            "allocation_id": "ca_video_1",
            "credit_balance_id": "cb_video_1",
            "amount_cents": 98,
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(
                video_jobs_module,
                "authorize_workbench_request",
                AsyncMock(return_value=self.fake_user),
            ), patch.object(
                video_jobs_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(
                video_jobs_module,
                "get_available_balance_cents",
                AsyncMock(return_value={"available_cents": 1000}),
            ), patch.object(
                video_jobs_module,
                "_charge_video_job_once",
                AsyncMock(
                    return_value={
                        "subscription_cents": 0,
                        "subscription_id": "",
                        "subscription_plan_id": "",
                        "traffic_pack_cents": 0,
                        "traffic_pack_debits": [],
                        "credit_cents": 98,
                        "credit_allocations": [allocation],
                        "legacy_cents": 0,
                    }
                ),
            ), patch.object(
                video_jobs_module,
                "_record_video_creation_usage",
                AsyncMock(),
            ):
                response = await client.post(
                    "/v1/videos/generations",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    json={
                        "model": "seedance-v2-720p",
                        "prompt": "Camera slowly pushes in",
                        "params": {"ratio": "16:9", "images": ["https://example.com/ref.jpg"]},
                    },
                )

        self.assertEqual(response.status_code, 202, response.text)
        job = next(iter(self.store.jobs.values()))
        self.assertEqual(job.credit_debit_cents, 98)
        self.assertEqual(json.loads(job.credit_allocations_json), [allocation])
        self.assertEqual(job.billable_sku, "seedance-v2-720p-video-task")

    async def test_wallet_failed_job_refunds_saved_allocations_exactly_once_without_scalar_credit(self) -> None:
        self.fake_user.balance = 1000
        job = VideoJob(
            id="job_wallet_fail_1",
            user_id=self.fake_user.id,
            api_key_id="k_video_user",
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            route_reason="catalog:seedance-v2-720p:upstream_direct",
            upstream_task_id="task_wallet_failed",
            request_payload_json="{}",
            charged_cents=98,
            credit_debit_cents=98,
            credit_allocations_json=json.dumps(
                [
                    {
                        "allocation_id": "ca_video_1",
                        "credit_balance_id": "cb_video_1",
                        "amount_cents": 98,
                    }
                ]
            ),
            refunded_cents=0,
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "refund_credit_allocations",
            AsyncMock(return_value={"refunded_cents": 98, "allocations": []}),
        ) as refund_wallet, patch.object(
            video_jobs_module,
            "_record_video_usage_daily",
            AsyncMock(),
        ), patch.object(
            video_jobs_module,
            "increment_finance_summary",
            AsyncMock(),
        ):
            await video_jobs_module._refund_failed_job_once(job, db)
            await video_jobs_module._refund_failed_job_once(job, db)

        refund_wallet.assert_awaited_once_with(
            db,
            user_id=self.fake_user.id,
            allocation_ids=["ca_video_1"],
            expected_allocations=[
                {
                    "allocation_id": "ca_video_1",
                    "credit_balance_id": "cb_video_1",
                    "amount_cents": 98,
                }
            ],
        )
        self.assertEqual(job.refunded_cents, 98)
        self.assertEqual(self.fake_user.balance, 1000)
        self.assertEqual(len(self.store.ledger_entries), 0)

    async def test_wallet_refund_missing_allocation_payload_fails_before_legacy_refund(self) -> None:
        self.fake_user.balance = 990
        job = VideoJob(
            id="job_wallet_missing_allocations",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=98,
            credit_debit_cents=88,
            credit_allocations_json=None,
            legacy_debit_cents=10,
            refunded_cents=0,
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "refund_credit_allocations",
            AsyncMock(),
        ) as refund_wallet:
            with self.assertRaises(video_jobs_module.BillingError):
                await video_jobs_module._refund_failed_job_once(job, db)

        refund_wallet.assert_not_awaited()
        self.assertEqual(job.refunded_cents, 0)
        self.assertEqual(self.fake_user.balance, 990)
        self.assertEqual(self.store.ledger_entries, [])

    async def test_wallet_refund_amount_mismatch_fails_before_legacy_refund(self) -> None:
        self.fake_user.balance = 990
        job = VideoJob(
            id="job_wallet_refund_mismatch",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=98,
            credit_debit_cents=88,
            credit_allocations_json=json.dumps(
                [
                    {
                        "allocation_id": "ca_video_mismatch",
                        "credit_balance_id": "cb_video_mismatch",
                        "amount_cents": 88,
                    }
                ]
            ),
            legacy_debit_cents=10,
            refunded_cents=0,
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "refund_credit_allocations",
            AsyncMock(return_value={"refunded_cents": 87, "allocations": []}),
        ):
            with self.assertRaises(video_jobs_module.BillingError):
                await video_jobs_module._refund_failed_job_once(job, db)

        self.assertEqual(job.refunded_cents, 0)
        self.assertEqual(self.fake_user.balance, 990)
        self.assertEqual(self.store.ledger_entries, [])

    async def test_stale_job_views_lock_and_recheck_before_refunding_once(self) -> None:
        self.fake_user.balance = 902
        canonical = VideoJob(
            id="job_concurrent_refund",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=98,
            legacy_debit_cents=98,
            refunded_cents=0,
        )
        self.store.jobs[canonical.id] = canonical
        stale_one = VideoJob(
            id=canonical.id,
            user_id=canonical.user_id,
            status=canonical.status,
            endpoint=canonical.endpoint,
            public_model=canonical.public_model,
            provider_model=canonical.provider_model,
            request_payload_json="{}",
            charged_cents=98,
            legacy_debit_cents=98,
            refunded_cents=0,
        )
        stale_two = VideoJob(
            id=canonical.id,
            user_id=canonical.user_id,
            status=canonical.status,
            endpoint=canonical.endpoint,
            public_model=canonical.public_model,
            provider_model=canonical.provider_model,
            request_payload_json="{}",
            charged_cents=98,
            legacy_debit_cents=98,
            refunded_cents=0,
        )
        db = _FakeVideoDBSession(self.store)

        with patch.object(video_jobs_module, "_record_video_usage_daily", AsyncMock()), patch.object(
            video_jobs_module,
            "increment_finance_summary",
            AsyncMock(),
        ):
            await video_jobs_module._refund_failed_job_once(stale_one, db)
            await video_jobs_module._refund_failed_job_once(stale_two, db)

        self.assertEqual(self.fake_user.balance, 1000)
        self.assertEqual(canonical.refunded_cents, 98)
        self.assertEqual(len(self.store.ledger_entries), 1)
        job_lock_queries = [query for query in db.queries if "coincoin_video_jobs" in str(query)]
        self.assertEqual(len(job_lock_queries), 2)
        self.assertTrue(all("FOR UPDATE" in str(query) for query in job_lock_queries))
        self.assertTrue(
            all(query.get_execution_options().get("populate_existing") for query in job_lock_queries)
        )

    async def test_missing_subscription_reference_fails_closed(self) -> None:
        job = VideoJob(
            id="job_missing_sub",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=10,
            subscription_debit_cents=10,
            subscription_id="sub_missing",
            refunded_cents=0,
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with self.assertRaises(video_jobs_module.BillingError):
            await video_jobs_module._refund_failed_job_once(job, db)

        self.assertEqual(job.refunded_cents, 0)
        self.assertEqual(self.store.ledger_entries, [])

    async def test_subscription_used_insufficient_blocks_mixed_refund_before_wallet_or_scalar(self) -> None:
        self.fake_user.balance = 990
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = UserSubscription(
            id="sub_underflow",
            user_id=self.fake_user.id,
            plan_id="monthly_light",
            status="active",
            period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=29),
            paid_until=now + timedelta(days=29),
            quota_cents=100,
            used_cents=5,
        )
        self.store.subscriptions[sub.id] = sub
        job = VideoJob(
            id="job_mixed_invalid_sub",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=70,
            subscription_debit_cents=10,
            subscription_id=sub.id,
            credit_debit_cents=50,
            credit_allocations_json=json.dumps(
                [{"allocation_id": "ca_mixed", "credit_balance_id": "cb_mixed", "amount_cents": 50}]
            ),
            legacy_debit_cents=10,
            refunded_cents=0,
            created_at=now,
            started_at=now,
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "refund_credit_allocations",
            AsyncMock(return_value={"refunded_cents": 50, "allocations": []}),
        ) as refund_wallet:
            with self.assertRaises(video_jobs_module.BillingError):
                await video_jobs_module._refund_failed_job_once(job, db)

        refund_wallet.assert_not_awaited()
        self.assertEqual(sub.used_cents, 5)
        self.assertEqual(self.fake_user.balance, 990)
        self.assertEqual(job.refunded_cents, 0)
        self.assertEqual(self.store.ledger_entries, [])

    async def test_subscription_rollover_refunds_old_period_debit_to_credit_and_wallet_once(self) -> None:
        sub = UserSubscription(
            id="sub_rollover_refund",
            user_id=self.fake_user.id,
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 7, 1),
            period_end=datetime(2026, 7, 31),
            paid_until=datetime(2026, 7, 31),
            quota_cents=100,
            used_cents=0,
        )
        self.store.subscriptions[sub.id] = sub
        allocation = {
            "allocation_id": "ca_rollover_wallet",
            "credit_balance_id": "cb_rollover_wallet",
            "amount_cents": 50,
        }
        job = VideoJob(
            id="job_rollover_refund",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=60,
            subscription_debit_cents=10,
            subscription_id=sub.id,
            subscription_plan_id="monthly_light",
            credit_debit_cents=50,
            credit_allocations_json=json.dumps([allocation]),
            refunded_cents=0,
            created_at=datetime(2026, 6, 15),
            started_at=datetime(2026, 6, 15),
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "refund_credit_allocations",
            AsyncMock(return_value={"refunded_cents": 50, "allocations": [allocation]}),
        ) as refund_wallet, patch.object(
            video_jobs_module,
            "grant_permanent_credit",
            AsyncMock(return_value=SimpleNamespace(remaining_cents=10)),
        ) as grant_fallback, patch.object(
            video_jobs_module,
            "_record_video_usage_daily",
            AsyncMock(),
        ), patch.object(
            video_jobs_module,
            "increment_finance_summary",
            AsyncMock(),
        ):
            await video_jobs_module._refund_failed_job_once(job, db)
            await video_jobs_module._refund_failed_job_once(job, db)

        refund_wallet.assert_awaited_once_with(
            db,
            user_id=self.fake_user.id,
            allocation_ids=[allocation["allocation_id"]],
            expected_allocations=[allocation],
        )
        grant_fallback.assert_awaited_once_with(
            db,
            user_id=self.fake_user.id,
            source_type=video_jobs_module.VIDEO_SUBSCRIPTION_REFUND_SOURCE_TYPE,
            source_id=job.id,
            amount_cents=10,
            product_id="monthly_light",
        )
        self.assertEqual(sub.used_cents, 0)
        self.assertEqual(job.refunded_cents, 60)
        self.assertEqual(len(self.store.ledger_entries), 1)
        self.assertEqual(self.store.ledger_entries[0].entry_type, "usage_subscription_refund_credit")

    async def test_subscription_rollover_never_decrements_usage_from_new_period(self) -> None:
        sub = UserSubscription(
            id="sub_rollover_used_refund",
            user_id=self.fake_user.id,
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 7, 1),
            period_end=datetime(2026, 7, 31),
            paid_until=datetime(2026, 7, 31),
            quota_cents=100,
            used_cents=20,
        )
        self.store.subscriptions[sub.id] = sub
        job = VideoJob(
            id="job_rollover_used_refund",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=10,
            subscription_debit_cents=10,
            subscription_id=sub.id,
            subscription_plan_id="monthly_light",
            refunded_cents=0,
            created_at=datetime(2026, 6, 15),
            started_at=datetime(2026, 6, 15),
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "grant_permanent_credit",
            AsyncMock(return_value=SimpleNamespace(remaining_cents=10)),
        ) as grant_fallback, patch.object(
            video_jobs_module,
            "_record_video_usage_daily",
            AsyncMock(),
        ), patch.object(
            video_jobs_module,
            "increment_finance_summary",
            AsyncMock(),
        ):
            await video_jobs_module._refund_failed_job_once(job, db)

        grant_fallback.assert_awaited_once()
        self.assertEqual(sub.used_cents, 20)
        self.assertEqual(job.refunded_cents, 10)

    async def test_expired_unrolled_subscription_refunds_debit_to_permanent_credit(self) -> None:
        sub = UserSubscription(
            id="sub_expired_refund",
            user_id=self.fake_user.id,
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 5, 1),
            period_end=datetime(2026, 5, 31),
            paid_until=datetime(2026, 5, 31),
            quota_cents=100,
            used_cents=20,
        )
        self.store.subscriptions[sub.id] = sub
        job = VideoJob(
            id="job_expired_refund",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=10,
            subscription_debit_cents=10,
            subscription_id=sub.id,
            subscription_plan_id="monthly_light",
            refunded_cents=0,
            created_at=datetime(2026, 5, 15),
            started_at=datetime(2026, 5, 15),
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "grant_permanent_credit",
            AsyncMock(return_value=SimpleNamespace(remaining_cents=10)),
        ) as grant_fallback, patch.object(
            video_jobs_module,
            "_record_video_usage_daily",
            AsyncMock(),
        ), patch.object(
            video_jobs_module,
            "increment_finance_summary",
            AsyncMock(),
        ):
            await video_jobs_module._refund_failed_job_once(job, db)

        grant_fallback.assert_awaited_once()
        self.assertEqual(sub.used_cents, 20)
        self.assertEqual(job.refunded_cents, 10)

    async def test_shortened_paid_until_refunds_debit_to_permanent_credit(self) -> None:
        sub = UserSubscription(
            id="sub_shortened_paid_until_refund",
            user_id=self.fake_user.id,
            plan_id="monthly_light",
            status="active",
            period_start=datetime(2026, 7, 1),
            period_end=datetime(2026, 7, 31),
            paid_until=datetime(2026, 7, 10),
            quota_cents=100,
            used_cents=20,
        )
        self.store.subscriptions[sub.id] = sub
        job = VideoJob(
            id="job_shortened_paid_until_refund",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=10,
            subscription_debit_cents=10,
            subscription_id=sub.id,
            subscription_plan_id="monthly_light",
            refunded_cents=0,
            created_at=datetime(2026, 7, 5),
            started_at=datetime(2026, 7, 5),
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "grant_permanent_credit",
            AsyncMock(return_value=SimpleNamespace(remaining_cents=10)),
        ) as grant_fallback, patch.object(
            video_jobs_module,
            "_record_video_usage_daily",
            AsyncMock(),
        ), patch.object(
            video_jobs_module,
            "increment_finance_summary",
            AsyncMock(),
        ):
            await video_jobs_module._refund_failed_job_once(job, db)

        grant_fallback.assert_awaited_once()
        self.assertEqual(sub.used_cents, 20)
        self.assertEqual(job.refunded_cents, 10)

    async def test_negative_legacy_total_blocks_mixed_refund_before_wallet(self) -> None:
        self.fake_user.balance = 1000
        job = VideoJob(
            id="job_negative_legacy_total",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            request_payload_json="{}",
            charged_cents=99,
            credit_debit_cents=100,
            credit_allocations_json=json.dumps(
                [{"allocation_id": "ca_negative", "credit_balance_id": "cb_negative", "amount_cents": 100}]
            ),
            legacy_debit_cents=-1,
            refunded_cents=0,
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        with patch.object(
            video_jobs_module,
            "refund_credit_allocations",
            AsyncMock(return_value={"refunded_cents": 100, "allocations": []}),
        ) as refund_wallet:
            with self.assertRaises(video_jobs_module.BillingError):
                await video_jobs_module._refund_failed_job_once(job, db)

        refund_wallet.assert_not_awaited()
        self.assertEqual(self.fake_user.balance, 1000)
        self.assertEqual(job.refunded_cents, 0)
        self.assertEqual(self.store.ledger_entries, [])

    async def test_charged_total_mismatch_blocks_all_refund_sources_before_mutation(self) -> None:
        for suffix, charged_cents in [("under", 129), ("over", 131)]:
            with self.subTest(suffix=suffix):
                self.fake_user.balance = 990
                sub = UserSubscription(
                    id=f"sub_reconcile_{suffix}",
                    user_id=self.fake_user.id,
                    plan_id="monthly_light",
                    status="active",
                    quota_cents=100,
                    used_cents=10,
                )
                pack = TrafficPackBalance(
                    id=f"tp_reconcile_{suffix}",
                    user_id=self.fake_user.id,
                    product_id="addon_boost",
                    status="active",
                    original_cents=100,
                    remaining_cents=90,
                    expires_at=datetime(2026, 8, 1),
                )
                self.store.subscriptions[sub.id] = sub
                self.store.traffic_packs[pack.id] = pack
                job = VideoJob(
                    id=f"job_reconcile_{suffix}",
                    user_id=self.fake_user.id,
                    status=video_jobs_module.JOB_STATUS_FAILED,
                    endpoint="videos/generations",
                    public_model="seedance-v2-720p",
                    provider_model="seedance-v2-720p",
                    request_payload_json="{}",
                    charged_cents=charged_cents,
                    subscription_debit_cents=10,
                    subscription_id=sub.id,
                    traffic_pack_debit_cents=10,
                    traffic_pack_debits_json=json.dumps(
                        [{"id": pack.id, "product_id": pack.product_id, "cents": 10}]
                    ),
                    credit_debit_cents=100,
                    credit_allocations_json=json.dumps(
                        [{"allocation_id": f"ca_{suffix}", "credit_balance_id": f"cb_{suffix}", "amount_cents": 100}]
                    ),
                    legacy_debit_cents=10,
                    refunded_cents=0,
                )
                self.store.jobs[job.id] = job
                db = _FakeVideoDBSession(self.store)

                with patch.object(
                    video_jobs_module,
                    "refund_credit_allocations",
                    AsyncMock(return_value={"refunded_cents": 100, "allocations": []}),
                ) as refund_wallet:
                    with self.assertRaises(video_jobs_module.BillingError):
                        await video_jobs_module._refund_failed_job_once(job, db)

                refund_wallet.assert_not_awaited()
                self.assertEqual(sub.used_cents, 10)
                self.assertEqual(pack.remaining_cents, 90)
                self.assertEqual(self.fake_user.balance, 990)
                self.assertEqual(job.refunded_cents, 0)
                self.assertEqual(self.store.ledger_entries, [])

    async def test_refresh_locks_only_after_upstream_result_before_terminal_transition(self) -> None:
        job = VideoJob(
            id="job_refresh_terminal_lock",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_RUNNING,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="seedance-v2-720p",
            upstream_task_id="task_refresh_terminal_lock",
            request_payload_json="{}",
            charged_cents=0,
            refunded_cents=0,
        )
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)

        class _NoPreHttpLockClient:
            async def post(inner_self, _url, **_kwargs):
                self.assertFalse(any("FOR UPDATE" in str(query) for query in db.queries))
                return _FakeSeedanceResponse(
                    {"code": 0, "data": {"task_id": job.upstream_task_id, "status": "failed"}}
                )

        with patch.object(
            video_jobs_module,
            "get_http_client",
            AsyncMock(return_value=_NoPreHttpLockClient()),
        ):
            refreshed = await video_jobs_module._refresh_video_job(job, db)

        self.assertIs(refreshed, job)
        self.assertEqual(job.status, video_jobs_module.JOB_STATUS_FAILED)
        job_lock_queries = [query for query in db.queries if "coincoin_video_jobs" in str(query)]
        self.assertEqual(len(job_lock_queries), 1)
        self.assertIn("FOR UPDATE", str(job_lock_queries[0]))

    async def test_missing_backend_locks_and_rechecks_canonical_terminal_job(self) -> None:
        canonical = VideoJob(
            id="job_missing_backend_stale",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_COMPLETED,
            endpoint="videos/generations",
            public_model="seedance-v2-720p",
            provider_model="missing-provider",
            request_payload_json="{}",
            charged_cents=0,
            refunded_cents=0,
        )
        self.store.jobs[canonical.id] = canonical
        stale = VideoJob(
            id=canonical.id,
            user_id=canonical.user_id,
            status=video_jobs_module.JOB_STATUS_RUNNING,
            endpoint=canonical.endpoint,
            public_model=canonical.public_model,
            provider_model=canonical.provider_model,
            request_payload_json="{}",
            charged_cents=0,
            refunded_cents=0,
        )
        db = _FakeVideoDBSession(self.store)

        with patch.object(video_jobs_module, "_backend_for_job", return_value=None):
            refreshed = await video_jobs_module._refresh_video_job(stale, db)

        self.assertIs(refreshed, canonical)
        self.assertEqual(canonical.status, video_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(stale.status, video_jobs_module.JOB_STATUS_RUNNING)
        job_lock_queries = [query for query in db.queries if "coincoin_video_jobs" in str(query)]
        self.assertEqual(len(job_lock_queries), 1)
        self.assertIn("FOR UPDATE", str(job_lock_queries[0]))

    async def test_invalid_traffic_pack_payloads_fail_closed(self) -> None:
        scenarios = [
            ("bad_json", "{", 20),
            (
                "sum_mismatch",
                json.dumps([{"id": "tp_1", "product_id": "addon_boost", "cents": 19}]),
                20,
            ),
        ]
        for suffix, payload, saved_total in scenarios:
            with self.subTest(suffix=suffix):
                job = VideoJob(
                    id=f"job_pack_{suffix}",
                    user_id=self.fake_user.id,
                    status=video_jobs_module.JOB_STATUS_FAILED,
                    endpoint="videos/generations",
                    public_model="seedance-v2-720p",
                    provider_model="seedance-v2-720p",
                    request_payload_json="{}",
                    charged_cents=saved_total,
                    traffic_pack_debit_cents=saved_total,
                    traffic_pack_debits_json=payload,
                    refunded_cents=0,
                )
                self.store.jobs[job.id] = job
                db = _FakeVideoDBSession(self.store)
                with self.assertRaises(video_jobs_module.BillingError):
                    await video_jobs_module._refund_failed_job_once(job, db)
                self.assertEqual(job.refunded_cents, 0)

    async def test_missing_or_overflowing_traffic_pack_reference_fails_before_any_mutation(self) -> None:
        scenarios = [("missing", None), ("overflow", (95, 100))]
        for suffix, pack_values in scenarios:
            with self.subTest(suffix=suffix):
                pack_id = f"tp_{suffix}"
                if pack_values:
                    remaining, original = pack_values
                    self.store.traffic_packs[pack_id] = TrafficPackBalance(
                        id=pack_id,
                        user_id=self.fake_user.id,
                        product_id="addon_boost",
                        status="active",
                        original_cents=original,
                        remaining_cents=remaining,
                        expires_at=datetime(2026, 8, 1),
                    )
                job = VideoJob(
                    id=f"job_pack_ref_{suffix}",
                    user_id=self.fake_user.id,
                    status=video_jobs_module.JOB_STATUS_FAILED,
                    endpoint="videos/generations",
                    public_model="seedance-v2-720p",
                    provider_model="seedance-v2-720p",
                    request_payload_json="{}",
                    charged_cents=10,
                    traffic_pack_debit_cents=10,
                    traffic_pack_debits_json=json.dumps(
                        [{"id": pack_id, "product_id": "addon_boost", "cents": 10}]
                    ),
                    refunded_cents=0,
                )
                self.store.jobs[job.id] = job
                db = _FakeVideoDBSession(self.store)
                with self.assertRaises(video_jobs_module.BillingError):
                    await video_jobs_module._refund_failed_job_once(job, db)
                if pack_values:
                    self.assertEqual(self.store.traffic_packs[pack_id].remaining_cents, 95)
                self.assertEqual(job.refunded_cents, 0)

    def test_video_job_wallet_columns_match_orm_startup_ddl_and_migrations(self) -> None:
        self.assertIn("credit_debit_cents", VideoJob.__table__.c)
        self.assertIn("credit_allocations_json", VideoJob.__table__.c)
        self.assertIn("billable_sku", VideoJob.__table__.c)
        source = __import__("inspect").getsource(main_module._run_migrations)
        credit_column = VideoJob.__table__.c.credit_debit_cents
        self.assertFalse(credit_column.nullable)
        self.assertEqual(credit_column.default.arg, 0)
        self.assertIn(
            '(\"coincoin_video_jobs\", \"credit_debit_cents\", \"BIGINT NOT NULL DEFAULT 0\")',
            source,
        )
        self.assertIn('(\"coincoin_video_jobs\", \"credit_allocations_json\", \"LONGTEXT NULL\")', source)
        self.assertIn(
            '(\"coincoin_video_jobs\", \"billable_sku\", \"VARCHAR(128) DEFAULT \'\'\")',
            source,
        )
        self.assertIn("credit_debit_cents BIGINT NOT NULL DEFAULT 0", source)
        self.assertEqual(VideoJob.__table__.c.credit_allocations_json.type.__class__.__name__, "LONGTEXT")
        self.assertIn("credit_allocations_json LONGTEXT NULL", source)
        self.assertIn("billable_sku VARCHAR(128) DEFAULT ''", source)

    async def test_video_job_freezes_billable_sku_for_charge_and_refund_logs(self) -> None:
        resolved = registry.resolve_public_model("seedance-v2-720p", "videos/generations")
        frozen_sku = resolved.public_model.billable_sku
        request_log_factory = unittest.mock.Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))

        with patch.object(video_jobs_module, "RequestLog", request_log_factory):
            charge_log = video_jobs_module._create_video_request_log(
                user_id=self.fake_user.id,
                api_key_id="k_video_user",
                public_model=resolved.public_model,
                used_cfg=resolved.backend,
                route_reason=resolved.route_reason,
                duration_ms=1,
                status_code=200,
                upstream_request_id="req_charge_sku",
                cost_cents=98,
            )

        job = VideoJob(
            id="job_frozen_sku",
            user_id=self.fake_user.id,
            status=video_jobs_module.JOB_STATUS_FAILED,
            endpoint="videos/generations",
            public_model=resolved.public_model.public_id,
            billable_sku=frozen_sku,
            provider_model=resolved.backend.model_id,
            request_payload_json="{}",
            charged_cents=98,
            legacy_debit_cents=98,
            refunded_cents=0,
        )
        self.fake_user.balance = 902
        self.store.jobs[job.id] = job
        db = _FakeVideoDBSession(self.store)
        refund_log_factory = unittest.mock.Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))

        with patch.object(video_jobs_module, "RequestLog", refund_log_factory), patch.object(
            video_jobs_module,
            "_record_video_usage_daily",
            AsyncMock(),
        ), patch.object(video_jobs_module, "increment_finance_summary", AsyncMock()):
            await video_jobs_module._refund_failed_job_once(job, db)

        self.assertEqual(charge_log.billable_sku, frozen_sku)
        self.assertEqual(refund_log_factory.call_args.kwargs["billable_sku"], frozen_sku)

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
            with patch.object(video_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)), patch.object(
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

    async def test_create_video_generation_user_override_preserves_public_model_and_hides_backend_identity(self) -> None:
        fake_user = SimpleNamespace(
            id="u_video",
            _api_key_id="k_video_user",
            balance=1000,
            _model_routing_overrides={
                "seedance-v2-720p": {
                    "public_model_id": "seedance-v2-720p",
                    "provider_model": "seedance-v2-720p-video",
                    "upstream_model": "seedance-v2-720p-video",
                    "enabled": True,
                }
            },
        )
        upstream_client = _RecordingSeedanceClient(
            [
                _FakeSeedanceResponse(
                    {
                        "code": 0,
                        "data": {
                            "task_id": "task_seedance_override_1",
                            "model": "seedance-v2-720p-video",
                            "status": "pending",
                        },
                    },
                    headers={"x-request-id": "req_seedance_override_1"},
                )
            ]
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(video_jobs_module, "authorize_workbench_request", AsyncMock(return_value=fake_user)), patch.object(
                video_jobs_module,
                "get_http_client",
                AsyncMock(return_value=upstream_client),
            ), patch.object(video_jobs_module.usage_buffer, "get_pending_cost", AsyncMock(return_value=0)), patch.object(
                video_jobs_module.usage_buffer,
                "add",
                AsyncMock(),
            ):
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
        self.assertEqual(payload["model"], "seedance-v2-720p")
        self.assertNotIn("provider_model", payload)
        self.assertNotIn("route_reason", payload)
        self.assertEqual(payload["result"]["data"]["model"], "seedance-v2-720p")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "seedance-v2-720p-video")

        job = next(iter(self.store.jobs.values()))
        self.assertEqual(job.public_model, "seedance-v2-720p")
        self.assertEqual(job.provider_model, "seedance-v2-720p-video")
        self.assertEqual(self.store.request_logs[0].customer_model_alias, "seedance-v2-720p")
        self.assertEqual(self.store.request_logs[0].provider_model, "seedance-v2-720p-video")

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
        self.assertNotIn("provider_model", payload)
        self.assertNotIn("route_reason", payload)
        self.assertEqual(self.store.jobs[job.id].attempt_count, 1)
        self.assertEqual(self.store.jobs[job.id].upstream_request_id, "req_seedance_query_1")
        self.assertEqual(upstream_client.calls[0]["url"], "https://api.wgspai.cn/v1/task/query")
        self.assertEqual(upstream_client.calls[0]["json"], {"task_id": job.upstream_task_id})
        self.assertEqual(len(self.store.media_artifacts), 1)
        artifact = self.store.media_artifacts[0]
        self.assertEqual(artifact.media_type, "video")
        self.assertEqual(artifact.endpoint, "videos/generations")
        self.assertEqual(artifact.url, "https://example.com/generated.mp4")
        self.assertEqual(artifact.user_id, self.fake_user.id)
        self.assertEqual(artifact.api_key_id, "k_video_user")
        self.assertEqual(artifact.source_type, "video_job")
        self.assertEqual(artifact.source_id, job.id)
        self.assertEqual(artifact.cost_cents, 98)

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
