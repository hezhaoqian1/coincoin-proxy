import asyncio
import json
import logging
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal, get_db
from . import gemini_cpa
from .models import ImageJob
from .media_store import record_media_artifacts_best_effort
from .router import registry as model_registry
from .station_runtime import public_model_pricing_kwargs
from .usage_buffer import usage_buffer
from .proxy import (
    _build_upstream_headers,
    _encode_multipart_form_data,
    _model_resolution_error_response,
    _openai_error_response,
    _parse_image_edit_form,
    _requested_image_count_from_pairs,
    _send_stream_request,
    _channel_usage_kwargs,
    _record_channel_failure,
    _record_channel_success,
    authenticate_user,
    authorize_workbench_request,
    extract_upstream_request_id,
    get_image_stream_client,
    _KEY_ID_ATTR,
)


logger = logging.getLogger("coincoin.image_jobs")

router = APIRouter(prefix="/openai/v1", tags=["image-jobs"])
openai_router = APIRouter(prefix="/v1", tags=["image-jobs"])

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"


def _job_storage_root() -> Path:
    root = Path(settings.image_job_storage_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_storage_dir(job_id: str) -> Path:
    return _job_storage_root() / job_id


def _job_response(job: ImageJob) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": job.id,
        "object": "image.job",
        "status": job.status,
        "endpoint": job.endpoint,
        "model": job.public_model,
        "provider_model": job.provider_model,
        "image_count": int(job.image_count or 0),
        "attempt_count": int(job.attempt_count or 0),
        "route_reason": job.route_reason,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
    if job.status == JOB_STATUS_COMPLETED and job.result_payload_json:
        try:
            payload["result"] = json.loads(job.result_payload_json)
        except Exception:
            payload["result"] = {"raw": job.result_payload_json}
    if job.status == JOB_STATUS_FAILED:
        payload["error"] = {
            "code": job.error_code or "image_job_failed",
            "message": job.error_message or "Image job failed",
        }
    return payload


def _build_job_manifest(
    *,
    requested_model: str,
    form_fields: List[Tuple[str, str]],
    file_fields: List[Tuple[str, Tuple[str, bytes, str]]],
) -> Dict[str, Any]:
    files: List[Dict[str, str]] = []
    for idx, (field_name, (filename, _, mime_type)) in enumerate(file_fields):
        safe_name = f"{idx:02d}-{Path(filename or 'upload.bin').name}"
        files.append(
            {
                "field_name": field_name,
                "filename": filename or "upload.bin",
                "stored_name": safe_name,
                "mime_type": mime_type or "application/octet-stream",
            }
        )
    return {
        "requested_model": requested_model,
        "form_fields": [[key, value] for key, value in form_fields],
        "files": files,
    }


def _store_job_files(
    *,
    job_id: str,
    file_fields: List[Tuple[str, Tuple[str, bytes, str]]],
) -> Path:
    job_dir = _job_storage_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest = _build_job_manifest(requested_model="", form_fields=[], file_fields=file_fields)
    for idx, (_, (_, content, _)) in enumerate(file_fields):
        stored_name = manifest["files"][idx]["stored_name"]
        (job_dir / stored_name).write_bytes(content)
    return job_dir


def _load_job_files(job: ImageJob, manifest: Dict[str, Any]) -> List[Tuple[str, Tuple[str, bytes, str]]]:
    job_dir = Path(job.storage_dir)
    file_entries = manifest.get("files") or []
    loaded: List[Tuple[str, Tuple[str, bytes, str]]] = []
    for item in file_entries:
        path = job_dir / str(item.get("stored_name") or "")
        if not path.is_file():
            raise FileNotFoundError(f"missing job input file: {path.name}")
        loaded.append(
            (
                str(item.get("field_name") or "image[]"),
                (
                    str(item.get("filename") or path.name),
                    path.read_bytes(),
                    str(item.get("mime_type") or "application/octet-stream"),
                ),
            )
        )
    return loaded


def _cleanup_job_storage(storage_dir: str) -> None:
    if not storage_dir:
        return
    shutil.rmtree(storage_dir, ignore_errors=True)


def _supports_async_gemini_job(public_model) -> bool:
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    return public_model.provider_name.strip().lower() == "google" and delivery_lane in {"gateway", gemini_cpa.DELIVERY_LANE}


async def _mark_job_failed(
    job_id: str,
    *,
    code: str,
    message: str,
    duration_ms: int = 0,
) -> None:
    async with SessionLocal() as session:
        job = await session.get(ImageJob, job_id)
        if not job:
            return
        job.status = JOB_STATUS_FAILED
        job.error_code = code
        job.error_message = message
        job.duration_ms = duration_ms
        job.completed_at = datetime.utcnow()
        await session.commit()


async def _mark_job_completed(
    job_id: str,
    *,
    result_payload: Dict[str, Any],
    upstream_request_id: str,
    duration_ms: int,
    cost_cents: int = 0,
) -> None:
    async with SessionLocal() as session:
        job = await session.get(ImageJob, job_id)
        if not job:
            return
        completed_at = datetime.utcnow()
        job.status = JOB_STATUS_COMPLETED
        job.result_payload_json = json.dumps(result_payload, ensure_ascii=False)
        job.upstream_request_id = upstream_request_id
        job.duration_ms = duration_ms
        job.completed_at = completed_at
        await record_media_artifacts_best_effort(
            session,
            user_id=job.user_id,
            api_key_id=getattr(job, "api_key_id", "") or None,
            media_type="image",
            endpoint="image-jobs/edits",
            model=job.public_model,
            provider_model=job.provider_model,
            payload=result_payload,
            status="completed",
            source_type="image_job",
            source_id=job.id,
            upstream_request_id=upstream_request_id,
            route_reason=job.route_reason,
            cost_cents=cost_cents,
            completed_at=completed_at,
        )
        await session.commit()


async def _process_image_edit_job(job_id: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(ImageJob, job_id)
        if not job:
            return
        manifest = json.loads(job.request_payload_json)

    requested_model = str(manifest.get("requested_model") or job.public_model or "").strip()
    try:
        resolved = model_registry.resolve_public_model(requested_model, "images/edits")
    except Exception as exc:
        await _mark_job_failed(job_id, code="model_resolution_failed", message=str(exc))
        return

    public_model = resolved.public_model
    used_cfg = resolved.backend
    used_route_reason = resolved.route_reason
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    if not _supports_async_gemini_job(public_model):
        await _mark_job_failed(
            job_id,
            code="unsupported_image_job_lane",
            message="Async image jobs currently support only Gemini image models on gateway or native CPA lanes.",
        )
        return

    try:
        file_fields = _load_job_files(job, manifest)
    except Exception as exc:
        await _mark_job_failed(job_id, code="job_input_missing", message=str(exc))
        return

    form_fields = [(str(key), str(value)) for key, value in (manifest.get("form_fields") or [])]
    started = time.monotonic()
    try:
        if delivery_lane == gemini_cpa.DELIVERY_LANE:
            channel = gemini_cpa.select_channel(public_model, used_cfg)
            payload = gemini_cpa.build_image_edit_payload(form_fields, file_fields, channel.provider_model)
            client = await get_image_stream_client()
            upstream = await _send_stream_request(
                client,
                "POST",
                gemini_cpa.chat_completions_url(channel),
                json=payload,
                headers=gemini_cpa.build_headers(channel),
            )
            try:
                upstream_body = await upstream.aread()
            finally:
                await upstream.aclose()
        else:
            channel = None
            upstream_form_fields = [(key, value) for key, value in form_fields if key != "model"]
            upstream_form_fields.append(("model", used_cfg.model_id))
            headers = _build_upstream_headers(used_cfg)
            upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/images/edits"
            multipart_body, multipart_content_type = _encode_multipart_form_data(upstream_form_fields, file_fields)
            headers["content-type"] = multipart_content_type

            stream_client = await get_image_stream_client()
            upstream = await _send_stream_request(
                stream_client,
                "POST",
                upstream_url,
                content=multipart_body,
                headers=headers,
            )
            try:
                upstream_body = await upstream.aread()
            finally:
                await upstream.aclose()
    except Exception as exc:
        if delivery_lane == gemini_cpa.DELIVERY_LANE and "channel" in locals() and channel is not None:
            gemini_cpa.record_failure(channel)
        _record_channel_failure(used_cfg, error_code="upstream_transport_error")
        await _mark_job_failed(job_id, code="upstream_transport_error", message=str(exc))
        return
    duration_ms = int((time.monotonic() - started) * 1000)
    upstream_request_id = extract_upstream_request_id(upstream.headers)

    try:
        payload = json.loads(upstream_body.decode("utf-8"))
    except Exception:
        _record_channel_failure(used_cfg, error_code="upstream_invalid_json")
        await _mark_job_failed(job_id, code="upstream_invalid_json", message="Upstream returned invalid JSON.", duration_ms=duration_ms)
        return

    if upstream.status_code >= 400:
        if delivery_lane == gemini_cpa.DELIVERY_LANE and "channel" in locals() and channel is not None and gemini_cpa.should_record_failure(upstream.status_code):
            gemini_cpa.record_failure(channel)
        _record_channel_failure(used_cfg, status_code=upstream.status_code)
        err = payload.get("error") if isinstance(payload, dict) else None
        message = str(err.get("message") or err) if isinstance(err, dict) else str(payload)
        await _mark_job_failed(job_id, code="upstream_error", message=message, duration_ms=duration_ms)
        return

    if delivery_lane == gemini_cpa.DELIVERY_LANE:
        payload = gemini_cpa.translate_image_response(payload if isinstance(payload, dict) else {})
        if "channel" in locals() and channel is not None:
            gemini_cpa.record_success(channel)
        _record_channel_success(used_cfg, duration_ms=duration_ms)

    data_items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data_items, list) or not data_items:
        _record_channel_failure(used_cfg, error_code="empty_image_result")
        await _mark_job_failed(
            job_id,
            code="empty_image_result",
            message="Image job completed without output images.",
            duration_ms=duration_ms,
        )
        return

    if delivery_lane != gemini_cpa.DELIVERY_LANE:
        _record_channel_success(used_cfg, duration_ms=duration_ms)
    await usage_buffer.add(
        job.user_id,
        api_key_id=getattr(job, "api_key_id", "") or "",
        requests=1,
        endpoint="image-jobs/edits",
        model=job.public_model,
        customer_model_alias=job.public_model,
        provider_model=public_model.provider_model or used_cfg.model_id,
        route_reason=used_route_reason,
        duration_ms=duration_ms,
        status_code=upstream.status_code,
        usage_unit_type="images",
        usage_unit_count=1,
        billable_sku=public_model.billable_sku or job.public_model,
        upstream_request_id=upstream_request_id,
        image_count=1,
        price_per_image_cents=public_model.price_per_image_cents,
        **_channel_usage_kwargs(used_cfg, channel if "channel" in locals() else None),
        **public_model_pricing_kwargs(public_model),
    )
    await _mark_job_completed(
        job_id,
        result_payload=payload if isinstance(payload, dict) else {"raw": payload},
        upstream_request_id=upstream_request_id,
        duration_ms=duration_ms,
        cost_cents=round(float(public_model.price_per_image_cents or 0.0)),
    )


async def process_pending_image_jobs(limit: int = 1) -> None:
    if not settings.image_jobs_enabled:
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(ImageJob.id)
            .where(ImageJob.status == JOB_STATUS_QUEUED)
            .order_by(ImageJob.created_at.asc())
            .limit(limit)
        )
        job_ids = [row[0] for row in result.all()]

    for job_id in job_ids:
        async with SessionLocal() as session:
            claim = await session.execute(
                update(ImageJob)
                .where(ImageJob.id == job_id, ImageJob.status == JOB_STATUS_QUEUED)
                .values(
                    status=JOB_STATUS_RUNNING,
                    started_at=datetime.utcnow(),
                    attempt_count=ImageJob.attempt_count + 1,
                )
            )
            await session.commit()
            if claim.rowcount != 1:
                continue

        try:
            await _process_image_edit_job(job_id)
        finally:
            async with SessionLocal() as session:
                job = await session.get(ImageJob, job_id)
                if job and job.status in {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED}:
                    _cleanup_job_storage(job.storage_dir)


async def image_job_loop(poll_interval: int) -> None:
    while True:
        try:
            await process_pending_image_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("image job loop iteration failed")
        await asyncio.sleep(max(1, poll_interval))


async def _create_image_edit_job(request: Request, db: AsyncSession) -> JSONResponse:
    if not settings.image_jobs_enabled:
        return _openai_error_response(
            "Async image jobs are disabled on this deployment.",
            code="image_jobs_disabled",
            error_type="server_error",
            status_code=503,
        )

    user = await authorize_workbench_request(request, db)
    try:
        requested_model, form_fields, file_fields = await _parse_image_edit_form(request)
    except Exception as exc:
        return _openai_error_response(
            f"Invalid multipart payload: {exc}",
            code="invalid_multipart_payload",
            status_code=400,
        )

    try:
        resolved = model_registry.resolve_public_model(requested_model, "images/edits")
    except Exception as exc:
        return _model_resolution_error_response(exc)

    public_model = resolved.public_model
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    if not _supports_async_gemini_job(public_model):
        return _openai_error_response(
            "Async image jobs currently support only Gemini image models on gateway or native CPA lanes.",
            code="unsupported_image_job_lane",
            status_code=400,
        )

    image_count = sum(1 for key, _ in file_fields if key in {"image", "image[]"})
    if image_count < 1:
        return _openai_error_response(
            "Image edit jobs require at least one image input.",
            code="missing_image_input",
            status_code=400,
        )
    if image_count > int(settings.image_job_async_max_inputs or 8):
        return _openai_error_response(
            f"Async image jobs currently support up to {settings.image_job_async_max_inputs} input images.",
            code="image_job_input_limit_exceeded",
            status_code=400,
            param="image",
        )
    if _requested_image_count_from_pairs(form_fields) > 1:
        return _openai_error_response(
            "Async image jobs currently support only one output image per job.",
            code="image_candidate_count_not_supported",
            status_code=400,
            param="n",
        )

    total_bytes = sum(len(content) for key, (_, content, _) in file_fields if key in {"image", "image[]"})
    if total_bytes > int(settings.image_job_max_total_bytes or 0):
        return _openai_error_response(
            f"Image job inputs exceed the configured {settings.image_job_max_total_bytes} byte limit.",
            code="image_job_input_bytes_exceeded",
            status_code=400,
            param="image",
        )

    job_id = secrets.token_hex(16)
    job_dir = _job_storage_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest = _build_job_manifest(
        requested_model=requested_model or public_model.public_id,
        form_fields=form_fields,
        file_fields=file_fields,
    )
    for idx, (_, (_, content, _)) in enumerate(file_fields):
        stored_name = manifest["files"][idx]["stored_name"]
        (job_dir / stored_name).write_bytes(content)

    job = ImageJob(
        id=job_id,
        user_id=user.id,
        api_key_id=getattr(user, _KEY_ID_ATTR, "") or None,
        status=JOB_STATUS_QUEUED,
        endpoint="images/edits",
        public_model=public_model.public_id,
        provider_model=public_model.provider_model or resolved.backend.model_id,
        route_reason=resolved.route_reason,
        image_count=image_count,
        request_payload_json=json.dumps(manifest, ensure_ascii=False),
        storage_dir=str(job_dir),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return JSONResponse(status_code=202, content=_job_response(job))


async def _get_image_job(job_id: str, request: Request, db: AsyncSession) -> JSONResponse:
    user = await authenticate_user(request, db)
    result = await db.execute(select(ImageJob).where(ImageJob.id == job_id, ImageJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if not job:
        return _openai_error_response(
            "Image job not found.",
            code="image_job_not_found",
            status_code=404,
        )
    return JSONResponse(content=_job_response(job))


@router.post("/image-jobs/edits")
async def create_image_edit_job(request: Request, db: AsyncSession = Depends(get_db)):
    return await _create_image_edit_job(request, db)


@openai_router.post("/image-jobs/edits")
async def create_image_edit_job_openai(request: Request, db: AsyncSession = Depends(get_db)):
    return await _create_image_edit_job(request, db)


@router.get("/image-jobs/{job_id}")
async def get_image_job(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _get_image_job(job_id, request, db)


@openai_router.get("/image-jobs/{job_id}")
async def get_image_job_openai(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _get_image_job(job_id, request, db)
