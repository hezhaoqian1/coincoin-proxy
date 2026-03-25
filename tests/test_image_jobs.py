import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.main import app
from app.models import ImageJob
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


class _FakeDBSession:
    def __init__(self, store: _JobStore) -> None:
        self.store = store

    def add(self, job: ImageJob) -> None:
        if not job.created_at:
            job.created_at = datetime.utcnow()
        self.store.jobs[job.id] = job

    async def commit(self) -> None:
        return None

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
    def __init__(self, payload, status_code: int = 200, headers: dict | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}

    async def aread(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    async def aclose(self) -> None:
        return None


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
                "default_image_model": "gemini-image",
                "models": [
                    {
                        "id": "gpt-5.4",
                        "owned_by": "openai",
                        "provider_name": "OpenAI",
                        "capabilities": ["chat/completions", "responses", "embeddings"],
                        "routing_mode": "legacy_auto",
                        "delivery_lane": "legacy",
                    },
                    {
                        "id": "gemini-image",
                        "owned_by": "google",
                        "provider_name": "Google",
                        "provider_model": "gemini-3.1-flash-image-preview",
                        "capabilities": ["images/generations", "images/edits"],
                        "routing_mode": "direct",
                        "delivery_lane": "gateway",
                        "upstream_model": "vertex-gemini-3.1-flash-image-preview",
                        "upstream_url": "https://gateway.example/v1",
                        "api_key": "gateway-key",
                        "auth_style": "bearer",
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
        self.fake_user = SimpleNamespace(id="u_test")

        async def fake_db():
            yield _FakeDBSession(self.store)

        app.dependency_overrides[image_jobs_module.get_db] = fake_db

    def tearDown(self) -> None:
        for key, value in self._originals.items():
            setattr(settings, key, value)
        registry._initialized = False
        app.dependency_overrides.pop(image_jobs_module.get_db, None)
        self._tmpdir.cleanup()

    async def test_create_image_edit_job_endpoint_queues_job(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch.object(image_jobs_module, "authorize_request", AsyncMock(return_value=self.fake_user)):
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
        manifest = json.loads(job.request_payload_json)
        self.assertEqual(len(manifest["files"]), 3)
        self.assertTrue(Path(job.storage_dir).is_dir())
        for file_info in manifest["files"]:
            self.assertTrue((Path(job.storage_dir) / file_info["stored_name"]).is_file())

    async def test_get_image_job_returns_existing_job(self) -> None:
        job = ImageJob(
            id="job_get_1",
            user_id=self.fake_user.id,
            status=image_jobs_module.JOB_STATUS_COMPLETED,
            endpoint="images/edits",
            public_model="gemini-image",
            provider_model="gemini-3.1-flash-image-preview",
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
            status=image_jobs_module.JOB_STATUS_RUNNING,
            endpoint="images/edits",
            public_model="gemini-image",
            provider_model="gemini-3.1-flash-image-preview",
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
                        "created": 1774449999,
                        "data": [{"b64_json": "edited-result"}],
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
        self.assertEqual(result_payload["data"][0]["b64_json"], "edited-result")
        self.assertEqual(updated.upstream_request_id, "req_image_job_1")
        self.assertEqual(len(upstream_client.calls), 1)
        self.assertEqual(upstream_client.calls[0]["url"], "https://gateway.example/v1/images/edits")
        self.assertEqual(dict(upstream_client.calls[0]["data"])["model"], "vertex-gemini-3.1-flash-image-preview")
        add_usage.assert_awaited_once()
        self.assertEqual(add_usage.await_args.kwargs["endpoint"], "image-jobs/edits")
        self.assertEqual(add_usage.await_args.kwargs["usage_unit_count"], 1)


if __name__ == "__main__":
    unittest.main()
