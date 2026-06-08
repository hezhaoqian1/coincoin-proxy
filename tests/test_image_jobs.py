import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.channel_router import ModelChannelRouteSnapshot, ProviderChannelSnapshot, channel_router
from app.config import settings
from app.main import app
import app.main as main_module
from app.models import ImageJob, MediaArtifact
from app.router import registry
import app.image_jobs as image_jobs_module


class _FakeExecuteResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return list(self._rows)


class _JobStore:
    def __init__(self) -> None:
        self.jobs = {}
        self.media_artifacts = []
        self.fail_commit = False
        self.rollbacks = 0


class _FakeDBSession:
    def __init__(self, store: _JobStore) -> None:
        self.store = store

    def add(self, item) -> None:
        if isinstance(item, ImageJob):
            if not item.created_at:
                item.created_at = datetime.utcnow()
            self.store.jobs[item.id] = item
            return
        if isinstance(item, MediaArtifact):
            self.store.media_artifacts.append(item)
            return
        raise AssertionError(f"unexpected add: {item!r}")

    async def commit(self) -> None:
        if self.store.fail_commit:
            raise RuntimeError("simulated image job insert failure")
        return None

    async def rollback(self) -> None:
        self.store.rollbacks += 1

    async def refresh(self, job: ImageJob) -> None:
        if not job.created_at:
            job.created_at = datetime.utcnow()

    async def get(self, model, key: str):
        return self.store.jobs.get(key)

    async def execute(self, statement):
        filters = {}
        for criterion in getattr(statement, "_where_criteria", ()):
            left = getattr(criterion, "left", None)
            right = getattr(criterion, "right", None)
            key = getattr(left, "name", None)
            value = getattr(right, "value", None)
            if key is not None:
                filters[key] = value

        selected_columns = list(getattr(statement, "selected_columns", []) or [])
        if len(selected_columns) == 1 and getattr(selected_columns[0], "name", None) == "id":
            matched = [
                job
                for job in self.store.jobs.values()
                if all(getattr(job, key, None) == value for key, value in filters.items())
            ]
            matched.sort(key=lambda item: item.created_at or datetime.min)
            limit_clause = getattr(statement, "_limit_clause", None)
            if limit_clause is not None and getattr(limit_clause, "value", None) is not None:
                matched = matched[: int(limit_clause.value)]
            return _FakeExecuteResult(rows=[(job.id,) for job in matched])

        for job in self.store.jobs.values():
            if all(getattr(job, key, None) == value for key, value in filters.items()):
                return _FakeExecuteResult(scalar=job)
        return _FakeExecuteResult(scalar=None)


class _FakeSessionContext:
    def __init__(self, store: _JobStore) -> None:
        self._session = _FakeDBSession(store)

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSessionFactory:
    def __init__(self, store: _JobStore) -> None:
        self.store = store

    def __call__(self):
        return _FakeSessionContext(self.store)


class _FakeUpstreamResponse:
    def __init__(self, payload, status_code: int = 200, headers: dict | None = None, text: str | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if "application/json" not in str(self.headers.get("content-type") or ""):
            raise json.JSONDecodeError("not json", self.text, 0)
        return self._payload

    async def aread(self) -> bytes:
        return self.text.encode("utf-8")

    async def aclose(self) -> None:
        return None


class ImageJobMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_migrations_cover_existing_image_job_schema(self) -> None:
        class _FakeMigrationConn:
            def __init__(self) -> None:
                self.statements: list[str] = []

            async def execute(self, statement) -> None:
                self.statements.append(str(statement))

        conn = _FakeMigrationConn()

        await main_module._run_migrations(conn)

        sql = "\n".join(conn.statements)
        for column in [
            "api_key_id",
            "endpoint",
            "public_model",
            "provider_model",
            "route_reason",
            "image_count",
            "upstream_request_id",
            "channel_id",
            "channel_type",
            "provider_platform",
            "provider_account_fingerprint",
            "duration_ms",
            "storage_dir",
            "started_at",
            "completed_at",
            "updated_at",
        ]:
            self.assertIn(f"ALTER TABLE coincoin_image_jobs ADD COLUMN {column} ", sql)
        self.assertIn(
            "CREATE INDEX ix_image_jobs_status_created ON coincoin_image_jobs (status, created_at)",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_image_jobs_channel_id ON coincoin_image_jobs (channel_id)",
            sql,
        )
        self.assertIn(
            "ALTER TABLE coincoin_image_jobs MODIFY COLUMN route_reason VARCHAR(128) DEFAULT ''",
            sql,
        )
        self.assertIn(
            "ALTER TABLE coincoin_request_logs MODIFY COLUMN route_reason VARCHAR(128) DEFAULT ''",
            sql,
        )
        self.assertIn("route_reason VARCHAR(128) DEFAULT ''", sql)

class _RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected upstream call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _RecordingStreamClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def build_request(self, method, url, **kwargs):
        request = {"method": method, "url": url, **kwargs}
        self.calls.append(request)
        return request

    async def send(self, request, stream=False):
        if not stream:
            raise AssertionError("expected stream=True")
        if not self.responses:
            raise AssertionError("unexpected upstream stream call")
        return self.responses.pop(0)


class ImageJobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._originals = {
            "model_catalog_json": settings.model_catalog_json,
            "image_jobs_enabled": settings.image_jobs_enabled,
            "image_job_storage_dir": settings.image_job_storage_dir,
            "image_job_sync_input_limit": settings.image_job_sync_input_limit,
            "image_job_async_max_inputs": settings.image_job_async_max_inputs,
            "image_job_max_total_bytes": settings.image_job_max_total_bytes,
        }
        settings.model_catalog_json = json.dumps(
            {
                "default_text_model": "gpt-5.4",
                "default_embedding_model": "text-embedding-3-small",
                "default_image_model": "gemini-image",
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
                        "id": "gpt-image-2",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "provider_model": "gpt-image-2",
                        "capabilities": ["images/generations", "images/edits"],
                        "routing_mode": "direct",
                        "delivery_lane": "upstream_direct",
                        "upstream_model": "gpt-image-2",
                        "upstream_url": "https://cliproxy.example/v1",
                        "api_key": "cliproxy-key",
                        "auth_style": "bearer",
                        "price_per_image_cents": 5.3,
                        "billable_sku": "openai-image",
                    },
                    {
                        "id": "gemini-image",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-3.1-flash-image",
                        "capabilities": ["images/generations", "images/edits"],
                        "routing_mode": "direct",
                        "delivery_lane": "cpa_gemini",
                        "upstream_model": "gemini-3.1-flash-image",
                        "upstream_url": "https://gemini-cpa.example/v1",
                        "api_key": "gemini-cpa-key",
                        "auth_style": "bearer",
                        "metadata": {"provider_platform": "cpa_gemini"},
                    },
                ],
            }
        )
        settings.image_jobs_enabled = True
        settings.image_job_sync_input_limit = 2
        settings.image_job_async_max_inputs = 8
        settings.image_job_max_total_bytes = 50 * 1024 * 1024
        self._tmpdir = tempfile.TemporaryDirectory()
        settings.image_job_storage_dir = self._tmpdir.name
        registry._initialized = False
        registry.init_from_settings()

        self.store = _JobStore()
        self.fake_user = SimpleNamespace(id="u_test", _api_key_id="k_image_user")

        async def fake_db():
            yield _FakeDBSession(self.store)

        app.dependency_overrides[image_jobs_module.get_db] = fake_db

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry._initialized = False
        channel_router.clear_snapshot()
        app.dependency_overrides.pop(image_jobs_module.get_db, None)
        self._tmpdir.cleanup()

    async def test_create_image_edit_job_endpoint_queues_job(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(image_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)):
                response = await client.post(
                    "/v1/image-jobs/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Blend these references", "n": "1"},
                    files=[
                        ("image", ("input-1.png", b"fake_image_data_1", "image/png")),
                        ("image", ("input-2.png", b"fake_image_data_2", "image/png")),
                        ("image", ("input-3.png", b"fake_image_data_3", "image/png")),
                    ],
                )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], image_jobs_module.JOB_STATUS_QUEUED)
        self.assertEqual(payload["image_count"], 3)
        self.assertEqual(payload["model"], "gemini-image")
        self.assertEqual(len(self.store.jobs), 1)

        job = next(iter(self.store.jobs.values()))
        self.assertEqual(job.api_key_id, "k_image_user")
        manifest = json.loads(job.request_payload_json)
        self.assertEqual(len(manifest["files"]), 3)
        self.assertTrue(Path(job.storage_dir).is_dir())
        for file_info in manifest["files"]:
            self.assertTrue((Path(job.storage_dir) / file_info["stored_name"]).is_file())

    async def test_create_image_edit_job_user_override_preserves_public_model_and_hides_backend_identity(self) -> None:
        self.fake_user._model_routing_overrides = {
            "gemini-image": {
                "public_model_id": "gemini-image",
                "provider_model": "gemini-3.1-flash-image-preview",
                "upstream_model": "gemini-3.1-flash-image-preview",
                "enabled": True,
            }
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(image_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)):
                response = await client.post(
                    "/v1/image-jobs/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "gemini-image", "prompt": "Blend these references", "n": "1"},
                    files=[
                        ("image", ("input-1.png", b"fake_image_data_1", "image/png")),
                        ("image", ("input-2.png", b"fake_image_data_2", "image/png")),
                        ("image", ("input-3.png", b"fake_image_data_3", "image/png")),
                    ],
                )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "gemini-image")
        self.assertNotIn("provider_model", payload)
        self.assertNotIn("route_reason", payload)

        job = next(iter(self.store.jobs.values()))
        self.assertEqual(job.public_model, "gemini-image")
        self.assertEqual(job.provider_model, "gemini-3.1-flash-image-preview")
        self.assertIn(":user_override", job.route_reason)

    async def test_create_image_edit_job_station_alias_preserves_display_and_billing_snapshot(self) -> None:
        resolved = registry.resolve_public_model("gemini-image", "images/edits")
        station_model = SimpleNamespace(
            resolved_model=resolved,
            display_model="stone-image-fast",
            station_id="st_1",
            station_alias="stone-image-fast",
            resolved_public_model="gemini-image",
            retail_input_per_million=0,
            retail_output_per_million=0,
            retail_price_per_image_cents=42.0,
            wholesale_input_per_million=0,
            wholesale_output_per_million=0,
            wholesale_price_per_image_cents=11.0,
            price_version=7,
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(image_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)), patch.object(
                image_jobs_module,
                "resolve_station_model_for_user",
                AsyncMock(return_value=station_model),
            ):
                response = await client.post(
                    "/v1/image-jobs/edits",
                    headers={"Authorization": "Bearer sk_cc_test"},
                    data={"model": "stone-image-fast", "prompt": "Blend these references", "n": "1"},
                    files=[
                        ("image", ("input-1.png", b"fake_image_data_1", "image/png")),
                        ("image", ("input-2.png", b"fake_image_data_2", "image/png")),
                        ("image", ("input-3.png", b"fake_image_data_3", "image/png")),
                    ],
                )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["model"], "stone-image-fast")

        job = next(iter(self.store.jobs.values()))
        self.assertEqual(job.public_model, "stone-image-fast")
        manifest = json.loads(job.request_payload_json)
        snapshot = manifest["coincoin_snapshot"]
        self.assertEqual(snapshot["display_model"], "stone-image-fast")
        self.assertEqual(snapshot["resolved_public_model"], "gemini-image")
        self.assertEqual(snapshot["retail_price_per_image_cents"], 42.0)
        self.assertEqual(snapshot["station_usage"]["station_alias"], "stone-image-fast")
        self.assertEqual(snapshot["station_usage"]["wholesale_price_per_image_cents"], 11.0)

    async def test_create_image_generation_job_endpoint_queues_job(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(image_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)):
                response = await client.post(
                    "/v1/image-jobs/generations",
                    headers={
                        "Authorization": "Bearer sk_cc_test",
                        "X-CoinCoin-Workbench": "1",
                    },
                    json={
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 3,
                        "size": "1024x1024",
                    },
                )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], image_jobs_module.JOB_STATUS_QUEUED)
        self.assertEqual(payload["endpoint"], "images/generations")
        self.assertEqual(payload["image_count"], 3)
        self.assertEqual(payload["model"], "gpt-image-2")
        job = next(iter(self.store.jobs.values()))
        self.assertEqual(job.api_key_id, "k_image_user")
        self.assertEqual(job.endpoint, "images/generations")
        self.assertEqual(job.provider_model, "gpt-image-2")
        manifest = json.loads(job.request_payload_json)
        self.assertEqual(manifest["payload"]["n"], 3)
        self.assertEqual(manifest["payload"]["prompt"], "A tiny black dot on a white background")
        self.assertEqual(manifest["coincoin_snapshot"]["retail_price_per_image_cents"], 5.3)

    async def test_create_image_generation_job_commit_failure_returns_structured_error(self) -> None:
        self.store.fail_commit = True

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(image_jobs_module, "authorize_workbench_request", AsyncMock(return_value=self.fake_user)):
                response = await client.post(
                    "/v1/image-jobs/generations",
                    headers={
                        "Authorization": "Bearer sk_cc_test",
                        "X-CoinCoin-Workbench": "1",
                    },
                    json={
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 3,
                        "size": "1024x1024",
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "image_job_create_failed")
        self.assertEqual(payload["error"]["type"], "server_error")
        self.assertEqual(self.store.rollbacks, 1)

    async def test_create_image_generation_job_early_failure_returns_structured_error(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(
                image_jobs_module,
                "authorize_workbench_request",
                AsyncMock(side_effect=RuntimeError("simulated early image job failure")),
            ):
                response = await client.post(
                    "/v1/image-jobs/generations",
                    headers={
                        "Authorization": "Bearer sk_cc_test",
                        "X-CoinCoin-Workbench": "1",
                    },
                    json={
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 3,
                        "size": "1024x1024",
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "image_job_request_failed")
        self.assertEqual(payload["error"]["type"], "server_error")
        self.assertEqual(self.store.rollbacks, 1)

    async def test_get_image_job_returns_existing_job(self) -> None:
        job = ImageJob(
            id="job_get_1",
            user_id=self.fake_user.id,
            status=image_jobs_module.JOB_STATUS_COMPLETED,
            endpoint="images/edits",
            public_model="gemini-image",
            provider_model="gemini-3.1-flash-image",
            route_reason="catalog:gemini-image:direct",
            image_count=3,
            request_payload_json="{}",
            result_payload_json=json.dumps({"data": [{"b64_json": "edited"}]}),
            storage_dir=self._tmpdir.name,
            created_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(image_jobs_module, "authenticate_user", AsyncMock(return_value=self.fake_user)):
                response = await client.get(
                    f"/v1/image-jobs/{job.id}",
                    headers={"Authorization": "Bearer sk_cc_test"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["id"], job.id)
        self.assertEqual(payload["status"], image_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(payload["result"]["data"][0]["b64_json"], "edited")
        self.assertNotIn("provider_model", payload)
        self.assertNotIn("route_reason", payload)

    async def test_process_image_edit_job_completes_and_records_usage(self) -> None:
        job_id = "job_worker_1"
        job_dir = Path(self._tmpdir.name) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "requested_model": "gemini-image",
            "form_fields": [["prompt", "Blend these images"], ["n", "1"], ["size", "1024x1024"]],
            "files": [
                {
                    "field_name": "image",
                    "filename": "input-1.png",
                    "stored_name": "00-input-1.png",
                    "mime_type": "image/png",
                },
                {
                    "field_name": "image",
                    "filename": "input-2.png",
                    "stored_name": "01-input-2.png",
                    "mime_type": "image/png",
                },
                {
                    "field_name": "image",
                    "filename": "input-3.png",
                    "stored_name": "02-input-3.png",
                    "mime_type": "image/png",
                },
            ],
        }
        for idx, file_info in enumerate(manifest["files"], start=1):
            (job_dir / file_info["stored_name"]).write_bytes(f"fake-image-{idx}".encode("utf-8"))

        job = ImageJob(
            id=job_id,
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/edits",
            public_model="gemini-image",
            provider_model="gemini-3.1-flash-image",
            route_reason="catalog:gemini-image:direct",
            image_count=3,
            request_payload_json=json.dumps(manifest),
            storage_dir=str(job_dir),
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job

        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "images": [
                                        {
                                            "image_url": {
                                                "url": "data:image/png;base64,ZWRpdGVkLXJlc3VsdA=="
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    headers={"x-request-id": "req_image_job_1"},
                )
            ]
        )

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)), patch.object(
            image_jobs_module,
            "get_image_stream_client",
            AsyncMock(return_value=upstream_client),
        ), patch.object(image_jobs_module.usage_buffer, "add", AsyncMock()) as add_usage:
            await image_jobs_module._process_image_edit_job(job.id)

        updated = self.store.jobs[job.id]
        self.assertEqual(updated.status, image_jobs_module.JOB_STATUS_COMPLETED)
        result_payload = json.loads(updated.result_payload_json)
        self.assertEqual(result_payload["data"][0]["b64_json"], "ZWRpdGVkLXJlc3VsdA==")
        self.assertEqual(updated.upstream_request_id, "req_image_job_1")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gemini-cpa.example/v1/chat/completions")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer gemini-cpa-key")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gemini-3.1-flash-image")
        self.assertEqual(upstream_client.calls[0]["json"]["modalities"], ["image", "text"])
        content_parts = upstream_client.calls[0]["json"]["messages"][0]["content"]
        self.assertEqual(content_parts[-1], {"type": "text", "text": "Blend these images"})
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["api_key_id"], "k_image_job")
        self.assertEqual(add_usage.await_args.kwargs["endpoint"], "image-jobs/edits")
        self.assertEqual(add_usage.await_args.kwargs["usage_unit_count"], 1)
        self.assertEqual(len(self.store.media_artifacts), 1)
        artifact = self.store.media_artifacts[0]
        self.assertEqual(artifact.media_type, "image")
        self.assertEqual(artifact.endpoint, "image-jobs/edits")
        self.assertTrue(artifact.url.startswith("/v1/media-artifacts/"))
        self.assertTrue(artifact.url.endswith("/content"))
        self.assertEqual(artifact.user_id, self.fake_user.id)
        self.assertEqual(artifact.api_key_id, "k_image_job")
        self.assertEqual(artifact.source_type, "image_job")
        self.assertEqual(artifact.source_id, job.id)

    async def test_process_image_generation_job_completes_and_records_usage_and_artifact(self) -> None:
        job = ImageJob(
            id="job_generation_worker_1",
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/generations",
            public_model="gpt-image-2",
            provider_model="gpt-image-2",
            route_reason="catalog:gpt-image-2:direct",
            image_count=1,
            request_payload_json=json.dumps(
                {
                    "requested_model": "gpt-image-2",
                    "payload": {
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 1,
                        "size": "1024x1024",
                    },
                    "coincoin_snapshot": {
                        "display_model": "gpt-image-2",
                        "resolved_public_model": "gpt-image-2",
                        "retail_price_per_image_cents": 5.3,
                    },
                }
            ),
            storage_dir="",
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {
                        "created": 1774449999,
                        "data": [{"url": "https://cdn.example/image.png"}],
                    },
                    headers={"x-request-id": "req_image_generation_job_1"},
                )
            ]
        )

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)), patch.object(
            image_jobs_module,
            "get_http_client",
            AsyncMock(return_value=upstream_client),
        ), patch.object(image_jobs_module.usage_buffer, "add", AsyncMock()) as add_usage:
            await image_jobs_module._process_image_generation_job(job.id)

        updated = self.store.jobs[job.id]
        self.assertEqual(updated.status, image_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(updated.upstream_request_id, "req_image_generation_job_1")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://cliproxy.example/v1/images/generations")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer cliproxy-key")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-image-2")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["endpoint"], "image-jobs/generations")
        self.assertEqual(add_usage.await_args.kwargs["usage_unit_count"], 1)
        self.assertEqual(add_usage.await_args.kwargs["price_per_image_cents"], 5.3)
        self.assertEqual(len(self.store.media_artifacts), 1)
        artifact = self.store.media_artifacts[0]
        self.assertEqual(artifact.endpoint, "image-jobs/generations")
        self.assertEqual(artifact.url, "https://cdn.example/image.png")
        self.assertEqual(artifact.cost_cents, 5)

    async def test_process_image_generation_job_splits_multi_image_openai_compatible_requests(self) -> None:
        job = ImageJob(
            id="job_generation_worker_multi",
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/generations",
            public_model="gpt-image-2",
            provider_model="gpt-image-2",
            route_reason="catalog:gpt-image-2:direct",
            image_count=3,
            request_payload_json=json.dumps(
                {
                    "requested_model": "gpt-image-2",
                    "payload": {
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 3,
                        "size": "1024x1024",
                    },
                    "coincoin_snapshot": {
                        "display_model": "gpt-image-2",
                        "resolved_public_model": "gpt-image-2",
                        "retail_price_per_image_cents": 5.3,
                    },
                }
            ),
            storage_dir="",
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse({"created": 1774449999, "data": [{"url": "https://cdn.example/image-1.png"}]}),
                _FakeUpstreamResponse({"created": 1774450000, "data": [{"url": "https://cdn.example/image-2.png"}]}),
                _FakeUpstreamResponse(
                    {"created": 1774450001, "data": [{"b64_json": "aW1hZ2UtMw=="}]},
                    headers={"x-request-id": "req_image_generation_multi_3"},
                ),
            ]
        )

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)), patch.object(
            image_jobs_module,
            "get_http_client",
            AsyncMock(return_value=upstream_client),
        ), patch.object(image_jobs_module.usage_buffer, "add", AsyncMock()) as add_usage:
            await image_jobs_module._process_image_generation_job(job.id)

        updated = self.store.jobs[job.id]
        self.assertEqual(updated.status, image_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(updated.upstream_request_id, "req_image_generation_multi_3")
        self.assertEqual(len(upstream_client.calls), 3)
        for call in upstream_client.calls:
            self.assertEqual(call["url"], "https://cliproxy.example/v1/images/generations")
            self.assertEqual(call["json"]["model"], "gpt-image-2")
            self.assertEqual(call["json"]["n"], 1)
        result_payload = json.loads(updated.result_payload_json)
        self.assertEqual(len(result_payload["data"]), 3)
        self.assertEqual(result_payload["data"][0]["url"], "https://cdn.example/image-1.png")
        self.assertEqual(result_payload["data"][1]["url"], "https://cdn.example/image-2.png")
        self.assertEqual(result_payload["data"][2]["b64_json"], "aW1hZ2UtMw==")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["usage_unit_count"], 3)
        self.assertEqual(add_usage.await_args.kwargs["image_count"], 3)
        self.assertEqual(len(self.store.media_artifacts), 3)
        self.assertEqual(self.store.media_artifacts[0].url, "https://cdn.example/image-1.png")
        self.assertEqual(self.store.media_artifacts[1].url, "https://cdn.example/image-2.png")
        self.assertTrue(self.store.media_artifacts[2].url.startswith("/v1/media-artifacts/"))

    async def test_process_image_generation_job_uses_stored_channel_snapshot(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_image_primary",
                    name="Image Primary",
                    base_url="https://image-channel.example/v1",
                    api_key="image-channel-key",
                    auth_style="bearer",
                    channel_type="openai_compatible",
                    provider_platform="xtokenmirror",
                    provider_account_fingerprint="fp_image_1",
                )
            ],
            [],
            version=1,
        )
        job = ImageJob(
            id="job_generation_channel_1",
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/generations",
            public_model="gpt-image-2",
            provider_model="gpt-image-2-channel",
            route_reason="catalog:gpt-image-2:direct:channel:ch_image_primary",
            channel_id="ch_image_primary",
            channel_type="openai_compatible",
            provider_platform="xtokenmirror",
            provider_account_fingerprint="fp_image_1",
            image_count=1,
            request_payload_json=json.dumps(
                {
                    "requested_model": "gpt-image-2",
                    "payload": {
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 1,
                        "size": "1024x1024",
                    },
                    "coincoin_snapshot": {
                        "display_model": "gpt-image-2",
                        "resolved_public_model": "gpt-image-2",
                        "retail_price_per_image_cents": 5.3,
                    },
                }
            ),
            storage_dir="",
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"created": 1774449999, "data": [{"url": "https://cdn.example/channel-image.png"}]},
                    headers={"x-request-id": "req_image_generation_channel_1"},
                )
            ]
        )

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)), patch.object(
            image_jobs_module,
            "get_http_client",
            AsyncMock(return_value=upstream_client),
        ), patch.object(image_jobs_module.usage_buffer, "add", AsyncMock()) as add_usage:
            await image_jobs_module._process_image_generation_job(job.id)

        self.assertEqual(self.store.jobs[job.id].status, image_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(upstream_client.calls[0]["url"], "https://image-channel.example/v1/images/generations")
        self.assertEqual(upstream_client.calls[0]["headers"]["authorization"], "Bearer image-channel-key")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-image-2-channel")
        self.assertEqual(add_usage.await_args.kwargs["channel_id"], "ch_image_primary")
        self.assertEqual(add_usage.await_args.kwargs["provider_platform"], "xtokenmirror")

    async def test_process_image_generation_job_records_non_json_upstream_error(self) -> None:
        job = ImageJob(
            id="job_generation_html_502",
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/generations",
            public_model="gpt-image-2",
            provider_model="gpt-image-2",
            route_reason="catalog:gpt-image-2:direct",
            image_count=1,
            request_payload_json=json.dumps(
                {
                    "requested_model": "gpt-image-2",
                    "payload": {
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 1,
                        "size": "1024x1024",
                    },
                    "coincoin_snapshot": {
                        "display_model": "gpt-image-2",
                        "resolved_public_model": "gpt-image-2",
                        "retail_price_per_image_cents": 5.3,
                    },
                }
            ),
            storage_dir="",
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {},
                    status_code=502,
                    headers={"content-type": "text/html"},
                    text="<html><title>502 Bad Gateway</title><body>cloudflare</body></html>",
                )
            ]
        )

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)), patch.object(
            image_jobs_module,
            "get_http_client",
            AsyncMock(return_value=upstream_client),
        ), patch.object(image_jobs_module.usage_buffer, "add", AsyncMock()) as add_usage:
            await image_jobs_module._process_image_generation_job(job.id)

        updated = self.store.jobs[job.id]
        self.assertEqual(updated.status, image_jobs_module.JOB_STATUS_FAILED)
        self.assertEqual(updated.error_code, "upstream_unexpected_content_type")
        self.assertIn("status=502", updated.error_message)
        self.assertIn("text/html", updated.error_message)
        self.assertIn("cloudflare", updated.error_message)
        add_usage.assert_not_awaited()
        self.assertEqual(self.store.media_artifacts, [])

    async def test_process_image_generation_job_fallbacks_to_backup_channel(self) -> None:
        channel_router.set_snapshot(
            [
                ProviderChannelSnapshot(
                    channel_id="ch_image_primary",
                    name="Image Primary",
                    base_url="https://primary-image.example/v1",
                    api_key="primary-key",
                    auth_style="bearer",
                    channel_type="openai_compatible",
                    provider_platform="xtokenmirror",
                    provider_account_fingerprint="fp_primary",
                ),
                ProviderChannelSnapshot(
                    channel_id="ch_image_backup",
                    name="Image Backup",
                    base_url="https://backup-image.example/v1",
                    api_key="backup-key",
                    auth_style="bearer",
                    channel_type="openai_compatible",
                    provider_platform="polaris",
                    provider_account_fingerprint="fp_backup",
                ),
            ],
            [
                ModelChannelRouteSnapshot(
                    route_id="rt_primary",
                    public_model_id="gpt-image-2",
                    endpoint="images/generations",
                    channel_id="ch_image_primary",
                    upstream_model="gpt-image-2-primary",
                    priority_override=0,
                ),
                ModelChannelRouteSnapshot(
                    route_id="rt_backup",
                    public_model_id="gpt-image-2",
                    endpoint="images/generations",
                    channel_id="ch_image_backup",
                    upstream_model="gpt-image-2-backup",
                    priority_override=1,
                ),
            ],
            version=2,
        )
        job = ImageJob(
            id="job_generation_channel_fallback",
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/generations",
            public_model="gpt-image-2",
            provider_model="gpt-image-2-primary",
            route_reason="catalog:gpt-image-2:direct:channel:ch_image_primary",
            channel_id="ch_image_primary",
            channel_type="openai_compatible",
            provider_platform="xtokenmirror",
            provider_account_fingerprint="fp_primary",
            image_count=1,
            request_payload_json=json.dumps(
                {
                    "requested_model": "gpt-image-2",
                    "payload": {
                        "model": "gpt-image-2",
                        "prompt": "A tiny black dot on a white background",
                        "n": 1,
                        "size": "1024x1024",
                    },
                    "coincoin_snapshot": {
                        "display_model": "gpt-image-2",
                        "resolved_public_model": "gpt-image-2",
                        "retail_price_per_image_cents": 5.3,
                    },
                }
            ),
            storage_dir="",
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job
        upstream_client = _RecordingClient(
            [
                _FakeUpstreamResponse(
                    {"error": {"message": "primary unavailable"}},
                    status_code=502,
                    headers={"x-request-id": "req_primary_failed"},
                ),
                _FakeUpstreamResponse(
                    {"created": 1774449999, "data": [{"url": "https://cdn.example/fallback-image.png"}]},
                    headers={"x-request-id": "req_backup_success"},
                ),
            ]
        )

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)), patch.object(
            image_jobs_module,
            "get_http_client",
            AsyncMock(return_value=upstream_client),
        ), patch.object(image_jobs_module.usage_buffer, "add", AsyncMock()) as add_usage:
            await image_jobs_module._process_image_generation_job(job.id)

        updated = self.store.jobs[job.id]
        self.assertEqual(updated.status, image_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(updated.upstream_request_id, "req_backup_success")
        self.assertEqual(updated.channel_id, "ch_image_backup")
        self.assertEqual(updated.provider_platform, "polaris")
        self.assertEqual(updated.provider_model, "gpt-image-2-backup")
        self.assertEqual(updated.route_reason, "channel_fallback:502")
        self.assertEqual(len(upstream_client.calls), 2)
        self.assertEqual(upstream_client.calls[0]["url"], "https://primary-image.example/v1/images/generations")
        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gpt-image-2-primary")
        self.assertEqual(upstream_client.calls[1]["url"], "https://backup-image.example/v1/images/generations")
        self.assertEqual(upstream_client.calls[1]["headers"]["authorization"], "Bearer backup-key")
        self.assertEqual(upstream_client.calls[1]["json"]["model"], "gpt-image-2-backup")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["route_reason"], "channel_fallback:502")
        self.assertEqual(add_usage.await_args.kwargs["channel_id"], "ch_image_backup")
        self.assertEqual(add_usage.await_args.kwargs["fallback_from_channel_id"], "ch_image_primary")
        self.assertEqual(add_usage.await_args.kwargs["provider_model"], "gpt-image-2-backup")
        self.assertEqual(len(self.store.media_artifacts), 1)
        self.assertEqual(self.store.media_artifacts[0].url, "https://cdn.example/fallback-image.png")

    async def test_process_image_edit_job_uses_stored_backend_snapshot(self) -> None:
        job_id = "job_worker_override"
        job_dir = Path(self._tmpdir.name) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "requested_model": "gemini-image",
            "coincoin_snapshot": {
                "display_model": "stone-image-fast",
                "resolved_public_model": "gemini-image",
                "retail_price_per_image_cents": 42.0,
                "public_pricing": {
                    "pricing_mode": "multiplier",
                    "model_multiplier": 1.0,
                    "output_multiplier": 1.0,
                    "cache_read_multiplier": 0.0,
                    "image_multiplier": 1.0,
                    "video_multiplier": 1.0,
                    "base_price_input_per_million": 0,
                    "base_price_output_per_million": 0,
                    "base_price_per_image_cents": 18.0,
                    "base_price_per_video_cents": 0.0,
                    "effective_cached_input_per_million": 0.0,
                    "price_version": 5,
                },
                "station_usage": {
                    "station_id": "st_1",
                    "station_alias": "stone-image-fast",
                    "resolved_public_model": "gemini-image",
                    "wholesale_price_input_per_million": 0,
                    "wholesale_price_output_per_million": 0,
                    "wholesale_price_per_image_cents": 11.0,
                    "price_version": 7,
                },
            },
            "form_fields": [["prompt", "Blend these images"], ["n", "1"]],
            "files": [
                {
                    "field_name": "image",
                    "filename": "input-1.png",
                    "stored_name": "00-input-1.png",
                    "mime_type": "image/png",
                },
                {
                    "field_name": "image",
                    "filename": "input-2.png",
                    "stored_name": "01-input-2.png",
                    "mime_type": "image/png",
                },
                {
                    "field_name": "image",
                    "filename": "input-3.png",
                    "stored_name": "02-input-3.png",
                    "mime_type": "image/png",
                },
            ],
        }
        for idx, file_info in enumerate(manifest["files"], start=1):
            (job_dir / file_info["stored_name"]).write_bytes(f"fake-image-{idx}".encode("utf-8"))

        job = ImageJob(
            id=job_id,
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/edits",
            public_model="stone-image-fast",
            provider_model="gemini-3.1-flash-image-preview",
            route_reason="catalog:gemini-image:cpa_gemini:user_override",
            image_count=3,
            request_payload_json=json.dumps(manifest),
            storage_dir=str(job_dir),
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job

        upstream_client = _RecordingStreamClient(
            [
                _FakeUpstreamResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "images": [
                                        {
                                            "image_url": {
                                                "url": "data:image/png;base64,edited-result"
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    headers={"x-request-id": "req_image_job_override"},
                )
            ]
        )

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)), patch.object(
            image_jobs_module,
            "get_image_stream_client",
            AsyncMock(return_value=upstream_client),
        ), patch.object(image_jobs_module.usage_buffer, "add", AsyncMock()) as add_usage:
            await image_jobs_module._process_image_edit_job(job.id)

        self.assertEqual(upstream_client.calls[0]["json"]["model"], "gemini-3.1-flash-image-preview")
        self.assertEqual(add_usage.await_args.kwargs["model"], "stone-image-fast")
        self.assertEqual(add_usage.await_args.kwargs["customer_model_alias"], "stone-image-fast")
        self.assertEqual(add_usage.await_args.kwargs["provider_model"], "gemini-3.1-flash-image-preview")
        self.assertEqual(add_usage.await_args.kwargs["route_reason"], "catalog:gemini-image:cpa_gemini:user_override")
        self.assertEqual(add_usage.await_args.kwargs["station_alias"], "stone-image-fast")
        self.assertEqual(add_usage.await_args.kwargs["resolved_public_model"], "gemini-image")
        self.assertEqual(add_usage.await_args.kwargs["price_per_image_cents"], 42.0)
        self.assertEqual(add_usage.await_args.kwargs["wholesale_price_per_image_cents"], 11.0)

    async def test_mark_image_job_completed_records_http_artifact(self) -> None:
        job = ImageJob(
            id="job_worker_http",
            user_id=self.fake_user.id,
            api_key_id="k_image_job",
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/edits",
            public_model="gemini-image",
            provider_model="gemini-3.1-flash-image",
            route_reason="catalog:gemini-image:direct",
            image_count=1,
            request_payload_json="{}",
            storage_dir=self._tmpdir.name,
            created_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
        )
        self.store.jobs[job.id] = job

        with patch.object(image_jobs_module, "SessionLocal", _FakeSessionFactory(self.store)):
            await image_jobs_module._mark_job_completed(
                job.id,
                result_payload={"data": [{"url": "https://example.com/image.png"}]},
                upstream_request_id="req_image_http_1",
                duration_ms=1200,
                cost_cents=18,
            )

        updated = self.store.jobs[job.id]
        self.assertEqual(updated.status, image_jobs_module.JOB_STATUS_COMPLETED)
        self.assertEqual(updated.upstream_request_id, "req_image_http_1")
        self.assertEqual(len(self.store.media_artifacts), 1)
        artifact = self.store.media_artifacts[0]
        self.assertEqual(artifact.media_type, "image")
        self.assertEqual(artifact.endpoint, "image-jobs/edits")
        self.assertEqual(artifact.url, "https://example.com/image.png")
        self.assertEqual(artifact.user_id, self.fake_user.id)
        self.assertEqual(artifact.api_key_id, "k_image_job")
        self.assertEqual(artifact.source_type, "image_job")
        self.assertEqual(artifact.source_id, job.id)
        self.assertEqual(artifact.cost_cents, 18)


if __name__ == "__main__":
    unittest.main()
