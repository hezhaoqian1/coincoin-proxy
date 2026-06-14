import asyncio
from copy import deepcopy
import json
import logging
import secrets
import shutil
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .channel_router import channel_router, should_record_failure as should_record_channel_failure
from .config import settings
from .db import SessionLocal, get_db
from . import gemini_cpa
from .models import ImageJob
from .media_store import record_media_artifacts_best_effort
from .router import ModelConfig, registry as model_registry
from .station_runtime import (
    public_model_pricing_kwargs,
    resolve_station_model_for_user,
    station_usage_kwargs,
)
from .user_model_overrides import apply_user_overrides_to_resolution
from .usage_buffer import usage_buffer
from .proxy import (
    IMAGE_UPSTREAM_TIMEOUT,
    _build_openai_image_upstream_url,
    _build_upstream_headers,
    _build_vertex_image_generation_payload,
    _encode_multipart_form_data,
    _model_resolution_error_response,
    _openai_error_response,
    _parse_image_edit_form,
    _post_with_retries,
    _requested_image_count_from_json,
    _requested_image_count_from_pairs,
    _send_stream_request,
    _translate_vertex_image_response,
    _unsupported_google_image_lane_error,
    _vertex_image_candidate_count_error,
    _channel_usage_kwargs,
    _record_channel_failure,
    _record_channel_success,
    authenticate_user,
    authorize_workbench_request,
    extract_upstream_request_id,
    get_http_client,
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
ROUTE_REASON_MAX_LEN = 128
IMAGE_JOB_CHANNEL_FALLBACK_MAX_ATTEMPTS = 16
IMAGE_JOB_CHANNEL_FALLBACK_RETRY_ERRORS = frozenset({
    "upstream_transport_error",
    "upstream_invalid_json",
    "upstream_unexpected_content_type",
    "empty_image_result",
})


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
        "image_count": int(job.image_count or 0),
        "attempt_count": int(job.attempt_count or 0),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
    if job.status == JOB_STATUS_COMPLETED and job.result_payload_json:
        try:
            payload["result"] = _public_image_result_payload(
                job.public_model,
                json.loads(job.result_payload_json),
            )
        except Exception:
            payload["result"] = {"raw": job.result_payload_json}
    if job.status == JOB_STATUS_FAILED:
        payload["error"] = {
            "code": job.error_code or "image_job_failed",
            "message": job.error_message or "Image job failed",
        }
    return payload


def _public_image_result_payload(public_model: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    public_payload = deepcopy(payload)
    if isinstance(public_payload.get("model"), str):
        public_payload["model"] = public_model
    data = public_payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("model"), str):
        data["model"] = public_model
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("model"), str):
                item["model"] = public_model
    return public_payload


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


def _manifest_snapshot_dict(manifest: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = manifest.get(key)
    return value if isinstance(value, dict) else {}


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


def _image_job_artifact_endpoint(job: ImageJob) -> str:
    endpoint = str(job.endpoint or "").strip()
    if endpoint == "images/generations":
        return "image-jobs/generations"
    if endpoint == "images/edits":
        return "image-jobs/edits"
    return endpoint or "image-jobs"


def _bounded_route_reason(value: str) -> str:
    return str(value or "").strip()[:ROUTE_REASON_MAX_LEN]


def _channel_attempted_ids(cfg) -> Tuple[str, ...]:
    values: List[str] = []
    for raw in (
        getattr(cfg, "fallback_from_channel_id", "") or "",
        getattr(cfg, "channel_id", "") or "",
    ):
        values.extend(item.strip() for item in str(raw or "").split(",") if item.strip())
    return tuple(dict.fromkeys(values))


def _image_job_channel_fallback_route_reason(reason: str) -> str:
    return _bounded_route_reason(f"channel_fallback:{str(reason or 'retry')[:40]}")


def _should_try_image_job_channel_fallback(
    cfg,
    *,
    status_code: int | None = None,
    error_code: str = "",
) -> bool:
    channel_id = str(getattr(cfg, "channel_id", "") or "").strip()
    if not channel_id or channel_id.startswith("system:"):
        return False
    if status_code in {401, 403}:
        return True
    if status_code is not None and should_record_channel_failure(int(status_code or 0)):
        return True
    if error_code and error_code in IMAGE_JOB_CHANNEL_FALLBACK_RETRY_ERRORS:
        return True
    return False


def _next_image_job_channel_fallback_config(
    public_model,
    previous_cfg,
    endpoint: str,
    *,
    reason: str,
    status_code: int | None = None,
):
    if not _should_try_image_job_channel_fallback(previous_cfg, status_code=status_code, error_code=reason):
        return None
    attempted = _channel_attempted_ids(previous_cfg)
    if len(attempted) >= IMAGE_JOB_CHANNEL_FALLBACK_MAX_ATTEMPTS:
        return None
    fallback_cfg = model_registry.resolve_channel_fallback(
        public_model,
        previous_cfg,
        endpoint,
        exclude_channel_ids=attempted,
    )
    if fallback_cfg is None:
        return None
    logger.warning(
        "image job provider channel fallback endpoint=%s from=%s to=%s reason=%s attempt=%s",
        endpoint,
        getattr(previous_cfg, "channel_id", ""),
        fallback_cfg.channel_id,
        reason,
        fallback_cfg.route_attempt,
    )
    return fallback_cfg


def _body_preview(text: str, limit: int = 300) -> str:
    preview = " ".join(str(text or "").split())
    return preview[:limit]


def _upstream_json_error_message(response, *, code: str, body_text: str = "") -> str:
    status_code = int(getattr(response, "status_code", 0) or 0)
    content_type = str((getattr(response, "headers", {}) or {}).get("content-type") or "unknown")
    if code == "upstream_unexpected_content_type":
        return f"Upstream returned non-JSON response (status={status_code}, content-type={content_type})."
    return f"Upstream returned invalid JSON (status={status_code}, content-type={content_type})."


def _parse_upstream_json_response(response, body: bytes | None = None) -> Tuple[Any, str, str]:
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type") or "")
    body_text = ""
    if body is not None:
        try:
            body_text = body.decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
    elif content_type and "application/json" not in content_type.lower():
        body_text = str(getattr(response, "text", "") or "")

    if content_type and "application/json" not in content_type.lower():
        code = "upstream_unexpected_content_type"
        return None, code, _upstream_json_error_message(response, code=code, body_text=body_text)

    try:
        if body is not None:
            return json.loads(body_text), "", ""
        return response.json(), "", ""
    except Exception:
        if not body_text:
            body_text = str(getattr(response, "text", "") or "")
        code = "upstream_invalid_json"
        return None, code, _upstream_json_error_message(response, code=code, body_text=body_text)


def _single_image_generation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    single_payload = dict(payload)
    single_payload["n"] = 1
    return single_payload


def _merge_image_generation_payloads(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(payloads[0]) if payloads else {"created": int(time.time()), "data": []}
    data_items: List[Any] = []
    for payload in payloads:
        items = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(items, list):
            data_items.extend(items)
    merged["data"] = data_items
    return merged


async def _post_openai_compatible_image_generation(
    *,
    client,
    upstream_url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
) -> Tuple[Any, Any, str, str]:
    requested_count = _requested_image_count_from_json(payload)
    if requested_count <= 1:
        upstream = await client.post(
            upstream_url,
            json=payload,
            headers=headers,
            timeout=IMAGE_UPSTREAM_TIMEOUT,
        )
        upstream_payload_json, failure_code, failure_message = _parse_upstream_json_response(upstream)
        return upstream, upstream_payload_json, failure_code, failure_message

    merged_payloads: List[Dict[str, Any]] = []
    last_upstream = None
    single_payload = _single_image_generation_payload(payload)
    for _ in range(requested_count):
        upstream = await client.post(
            upstream_url,
            json=single_payload,
            headers=headers,
            timeout=IMAGE_UPSTREAM_TIMEOUT,
        )
        last_upstream = upstream
        upstream_payload_json, failure_code, failure_message = _parse_upstream_json_response(upstream)
        if failure_code or upstream.status_code >= 400:
            return upstream, upstream_payload_json, failure_code, failure_message
        if not isinstance(upstream_payload_json, dict):
            return upstream, upstream_payload_json, "upstream_invalid_json", "Upstream returned invalid JSON."
        data_items = upstream_payload_json.get("data")
        if not isinstance(data_items, list) or not data_items:
            return upstream, upstream_payload_json, "empty_image_result", "Image job completed without output images."
        merged_payloads.append(upstream_payload_json)

    return last_upstream, _merge_image_generation_payloads(merged_payloads), "", ""


def _image_job_channel_metadata(cfg) -> Dict[str, str]:
    return {
        "channel_id": str(getattr(cfg, "channel_id", "") or ""),
        "channel_type": str(getattr(cfg, "channel_type", "") or ""),
        "provider_platform": str(getattr(cfg, "provider_platform", "") or ""),
        "provider_account_fingerprint": str(getattr(cfg, "provider_account_fingerprint", "") or ""),
    }


def _backend_for_image_job(job: ImageJob, endpoint: str, public_model_id: str):
    resolved = model_registry.resolve_public_model(public_model_id or job.public_model, endpoint)
    public_model = resolved.public_model
    channel_id = str(getattr(job, "channel_id", "") or "").strip()
    if channel_id and not channel_id.startswith("system:"):
        for channel in channel_router.list_channels():
            if channel.channel_id != channel_id:
                continue
            if not (channel.base_url and channel.api_key):
                break
            backend = ModelConfig(
                model_id=job.provider_model or resolved.backend.model_id,
                upstream_url=channel.base_url,
                api_key=channel.api_key,
                price_input_per_million=resolved.backend.price_input_per_million,
                price_output_per_million=resolved.backend.price_output_per_million,
                strip_unsupported=resolved.backend.strip_unsupported or public_model.strip_unsupported,
                auth_style=channel.auth_style or resolved.backend.auth_style,
                channel_id=channel.channel_id,
                channel_type=channel.channel_type,
                provider_platform=channel.provider_platform,
                provider_account_fingerprint=channel.provider_account_fingerprint,
                transform_profile=resolved.backend.transform_profile,
                cost_tier=channel.cost_tier,
            )
            return replace(resolved, backend=backend)
    return replace(
        resolved,
        backend=replace(
            resolved.backend,
            model_id=str(job.provider_model or resolved.backend.model_id or "").strip(),
        ),
    )


async def _run_image_job_handler(handler, request: Request, db: AsyncSession) -> JSONResponse:
    try:
        return await handler(request, db)
    except HTTPException:
        raise
    except Exception:
        logger.exception("image job request failed before response")
        try:
            await db.rollback()
        except Exception:
            logger.exception("failed to rollback image job request")
        return _openai_error_response(
            "Unable to create image job.",
            code="image_job_request_failed",
            error_type="server_error",
            status_code=500,
        )


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
    provider_model: str = "",
    route_reason: str = "",
    channel_metadata: Dict[str, str] | None = None,
) -> None:
    async with SessionLocal() as session:
        job = await session.get(ImageJob, job_id)
        if not job:
            return
        completed_at = datetime.utcnow()
        job.status = JOB_STATUS_COMPLETED
        if provider_model:
            job.provider_model = provider_model
        if route_reason:
            job.route_reason = route_reason
        for key, value in (channel_metadata or {}).items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.result_payload_json = json.dumps(result_payload, ensure_ascii=False)
        job.upstream_request_id = upstream_request_id
        job.duration_ms = duration_ms
        job.completed_at = completed_at
        await record_media_artifacts_best_effort(
            session,
            user_id=job.user_id,
            api_key_id=getattr(job, "api_key_id", "") or None,
            media_type="image",
            endpoint=_image_job_artifact_endpoint(job),
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


async def _process_image_generation_job(job_id: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(ImageJob, job_id)
        if not job:
            return
        manifest = json.loads(job.request_payload_json)

    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        await _mark_job_failed(job_id, code="invalid_job_payload", message="Image generation job payload is invalid.")
        return

    requested_model = str(manifest.get("requested_model") or job.public_model or payload.get("model") or "").strip()
    snapshot = _manifest_snapshot_dict(manifest, "coincoin_snapshot")
    display_model = str(snapshot.get("display_model") or job.public_model or requested_model or "").strip()
    resolved_public_model_id = str(
        snapshot.get("resolved_public_model") or job.public_model or requested_model or ""
    ).strip()
    pricing_snapshot = _manifest_snapshot_dict(snapshot, "public_pricing")
    station_snapshot = _manifest_snapshot_dict(snapshot, "station_usage")

    try:
        resolved = _backend_for_image_job(job, "images/generations", resolved_public_model_id)
    except Exception as exc:
        await _mark_job_failed(job_id, code="model_resolution_failed", message=str(exc))
        return

    public_model = resolved.public_model
    used_cfg = resolved.backend
    used_route_reason = _bounded_route_reason(job.route_reason or resolved.route_reason)
    is_google_image_generation = public_model.provider_name.strip().lower() == "google"
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    should_use_gateway_image_generation = is_google_image_generation and delivery_lane == "gateway"
    should_use_cpa_gemini_image_generation = is_google_image_generation and delivery_lane == gemini_cpa.DELIVERY_LANE
    should_use_direct_vertex = is_google_image_generation and delivery_lane == "vertex_direct"

    if (
        is_google_image_generation
        and not should_use_gateway_image_generation
        and not should_use_cpa_gemini_image_generation
        and not should_use_direct_vertex
    ):
        await _mark_job_failed(
            job_id,
            code="unsupported_image_delivery_lane",
            message=f"Unsupported Gemini image delivery lane: {(delivery_lane or 'unknown')}.",
        )
        return

    if should_use_direct_vertex and not settings.vertex_api_key:
        await _mark_job_failed(
            job_id,
            code="vertex_image_generation_not_configured",
            message="Gemini image generation requires COINCOIN_VERTEX_API_KEY on the CoinCoin control plane.",
        )
        return

    started = time.monotonic()
    upstream = None
    upstream_payload_json: Any = None
    cpa_channel = None
    failure_code = ""
    failure_message = ""
    while True:
        attempt_cfg = used_cfg
        attempt_route_reason = used_route_reason
        attempt_cpa_channel = None
        try:
            if should_use_gateway_image_generation:
                upstream_payload = dict(payload)
                upstream_payload["model"] = attempt_cfg.model_id
                upstream_payload.pop("model_provider", None)
                upstream_url = f"{attempt_cfg.upstream_url.rstrip('/')}/images/generations"
                headers = _build_upstream_headers(attempt_cfg)
                stream_client = await get_image_stream_client()
                upstream = await _send_stream_request(
                    stream_client,
                    "POST",
                    upstream_url,
                    json=upstream_payload,
                    headers=headers,
                )
                try:
                    upstream_body = await upstream.aread()
                finally:
                    await upstream.aclose()
                upstream_payload_json, failure_code, failure_message = _parse_upstream_json_response(upstream, upstream_body)
            elif should_use_cpa_gemini_image_generation:
                attempt_cpa_channel = gemini_cpa.select_channel(public_model, attempt_cfg)
                upstream_payload = gemini_cpa.build_image_generation_payload(payload, attempt_cpa_channel.provider_model)
                client = await get_http_client()
                upstream = await _post_with_retries(
                    client,
                    gemini_cpa.chat_completions_url(attempt_cpa_channel),
                    json_body=upstream_payload,
                    headers=gemini_cpa.build_headers(attempt_cpa_channel),
                )
                upstream_payload_json, failure_code, failure_message = _parse_upstream_json_response(upstream)
            elif should_use_direct_vertex:
                upstream_payload = _build_vertex_image_generation_payload(payload)
                upstream_url = (
                    f"{settings.vertex_gemini_api_base.rstrip('/')}/models/"
                    f"{attempt_cfg.model_id or public_model.provider_model}:generateContent"
                )
                client = await get_http_client()
                upstream = await _post_with_retries(
                    client,
                    upstream_url,
                    json_body=upstream_payload,
                    headers={"x-goog-api-key": settings.vertex_api_key, "content-type": "application/json"},
                )
                upstream_payload_json, failure_code, failure_message = _parse_upstream_json_response(upstream)
            else:
                upstream_payload = dict(payload)
                upstream_payload["model"] = attempt_cfg.model_id
                upstream_payload.pop("model_provider", None)
                upstream_url = _build_openai_image_upstream_url(attempt_cfg.upstream_url, "images/generations")
                client = await get_http_client()
                upstream, upstream_payload_json, failure_code, failure_message = await _post_openai_compatible_image_generation(
                    client=client,
                    upstream_url=upstream_url,
                    payload=upstream_payload,
                    headers=_build_upstream_headers(attempt_cfg),
                )
        except gemini_cpa.GeminiCpaChannelUnavailable as exc:
            await _mark_job_failed(job_id, code="gemini_cpa_channel_cooling_down", message=str(exc))
            return
        except httpx.TransportError as exc:
            if attempt_cpa_channel is not None:
                gemini_cpa.record_failure(attempt_cpa_channel)
            _record_channel_failure(attempt_cfg, error_code="upstream_transport_error")
            fallback_cfg = _next_image_job_channel_fallback_config(
                public_model,
                attempt_cfg,
                "images/generations",
                reason="upstream_transport_error",
            )
            if fallback_cfg is not None:
                used_cfg = fallback_cfg
                used_route_reason = _image_job_channel_fallback_route_reason("upstream_transport_error")
                continue
            await _mark_job_failed(job_id, code="upstream_transport_error", message=str(exc))
            return
        except Exception as exc:
            if attempt_cpa_channel is not None:
                gemini_cpa.record_failure(attempt_cpa_channel)
            _record_channel_failure(attempt_cfg, error_code="upstream_image_generation_error")
            await _mark_job_failed(job_id, code="upstream_image_generation_error", message=str(exc))
            return

        duration_ms = int((time.monotonic() - started) * 1000)
        if failure_code:
            _record_channel_failure(attempt_cfg, error_code=failure_code)
            fallback_cfg = _next_image_job_channel_fallback_config(
                public_model,
                attempt_cfg,
                "images/generations",
                reason=failure_code,
            )
            if fallback_cfg is not None:
                used_cfg = fallback_cfg
                used_route_reason = _image_job_channel_fallback_route_reason(failure_code)
                continue
            await _mark_job_failed(job_id, code=failure_code, message=failure_message, duration_ms=duration_ms)
            return

        if upstream.status_code >= 400:
            if attempt_cpa_channel is not None and gemini_cpa.should_record_failure(upstream.status_code):
                gemini_cpa.record_failure(attempt_cpa_channel)
            _record_channel_failure(attempt_cfg, status_code=upstream.status_code)
            fallback_cfg = _next_image_job_channel_fallback_config(
                public_model,
                attempt_cfg,
                "images/generations",
                reason=str(upstream.status_code),
                status_code=upstream.status_code,
            )
            if fallback_cfg is not None:
                used_cfg = fallback_cfg
                used_route_reason = _image_job_channel_fallback_route_reason(str(upstream.status_code))
                continue
            err = upstream_payload_json.get("error") if isinstance(upstream_payload_json, dict) else None
            message = str(err.get("message") or err) if isinstance(err, dict) else str(upstream_payload_json)
            await _mark_job_failed(job_id, code="upstream_error", message=message, duration_ms=duration_ms)
            return

        result_payload = upstream_payload_json if isinstance(upstream_payload_json, dict) else {}
        if should_use_cpa_gemini_image_generation:
            result_payload = gemini_cpa.translate_image_response(result_payload)
            if attempt_cpa_channel is not None:
                gemini_cpa.record_success(attempt_cpa_channel)
        elif should_use_direct_vertex:
            result_payload = _translate_vertex_image_response(result_payload)

        data_items = result_payload.get("data") if isinstance(result_payload, dict) else None
        if not isinstance(data_items, list) or not data_items:
            _record_channel_failure(attempt_cfg, error_code="empty_image_result")
            fallback_cfg = _next_image_job_channel_fallback_config(
                public_model,
                attempt_cfg,
                "images/generations",
                reason="empty_image_result",
            )
            if fallback_cfg is not None:
                used_cfg = fallback_cfg
                used_route_reason = _image_job_channel_fallback_route_reason("empty_image_result")
                continue
            await _mark_job_failed(
                job_id,
                code="empty_image_result",
                message="Image job completed without output images.",
                duration_ms=duration_ms,
            )
            return

        used_cfg = attempt_cfg
        used_route_reason = attempt_route_reason
        cpa_channel = attempt_cpa_channel
        break

    upstream_request_id = extract_upstream_request_id(upstream.headers)
    image_count = len(data_items)
    _record_channel_success(used_cfg, duration_ms=duration_ms)
    if "retail_price_per_image_cents" in snapshot:
        price_per_image_cents = float(snapshot.get("retail_price_per_image_cents") or 0.0)
    else:
        price_per_image_cents = float(public_model.price_per_image_cents or 0.0)
    pricing_kwargs = public_model_pricing_kwargs(public_model)
    if pricing_snapshot:
        pricing_kwargs = dict(pricing_snapshot)
    if station_snapshot:
        pricing_kwargs.update(station_snapshot)

    await usage_buffer.add(
        job.user_id,
        api_key_id=getattr(job, "api_key_id", "") or "",
        requests=1,
        endpoint="image-jobs/generations",
        model=display_model,
        customer_model_alias=display_model,
        provider_model=str(used_cfg.model_id or job.provider_model or public_model.provider_model or "").strip(),
        route_reason=used_route_reason,
        duration_ms=duration_ms,
        status_code=upstream.status_code,
        usage_unit_type="images",
        usage_unit_count=image_count,
        billable_sku=public_model.billable_sku or display_model,
        upstream_request_id=upstream_request_id,
        image_count=image_count,
        price_per_image_cents=price_per_image_cents,
        **_channel_usage_kwargs(used_cfg, cpa_channel),
        **pricing_kwargs,
    )
    await _mark_job_completed(
        job_id,
        result_payload=result_payload,
        upstream_request_id=upstream_request_id,
        duration_ms=duration_ms,
        cost_cents=round(price_per_image_cents * image_count),
        provider_model=str(used_cfg.model_id or job.provider_model or public_model.provider_model or "").strip(),
        route_reason=used_route_reason,
        channel_metadata=_image_job_channel_metadata(used_cfg),
    )


async def _process_image_edit_job(job_id: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(ImageJob, job_id)
        if not job:
            return
        manifest = json.loads(job.request_payload_json)

    requested_model = str(manifest.get("requested_model") or job.public_model or "").strip()
    snapshot = _manifest_snapshot_dict(manifest, "coincoin_snapshot")
    display_model = str(snapshot.get("display_model") or job.public_model or requested_model or "").strip()
    resolved_public_model_id = str(
        snapshot.get("resolved_public_model") or job.public_model or requested_model or ""
    ).strip()
    pricing_snapshot = _manifest_snapshot_dict(snapshot, "public_pricing")
    station_snapshot = _manifest_snapshot_dict(snapshot, "station_usage")
    try:
        resolved = _backend_for_image_job(job, "images/edits", resolved_public_model_id)
    except Exception as exc:
        await _mark_job_failed(job_id, code="model_resolution_failed", message=str(exc))
        return

    public_model = resolved.public_model
    used_cfg = resolved.backend
    used_route_reason = _bounded_route_reason(job.route_reason or resolved.route_reason)
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
    if "retail_price_per_image_cents" in snapshot:
        price_per_image_cents = float(snapshot.get("retail_price_per_image_cents") or 0.0)
    else:
        price_per_image_cents = float(public_model.price_per_image_cents or 0.0)
    pricing_kwargs = public_model_pricing_kwargs(public_model)
    if pricing_snapshot:
        pricing_kwargs = dict(pricing_snapshot)
    if station_snapshot:
        pricing_kwargs.update(station_snapshot)

    await usage_buffer.add(
        job.user_id,
        api_key_id=getattr(job, "api_key_id", "") or "",
        requests=1,
        endpoint="image-jobs/edits",
        model=display_model,
        customer_model_alias=display_model,
        provider_model=str(job.provider_model or used_cfg.model_id or public_model.provider_model or "").strip(),
        route_reason=used_route_reason,
        duration_ms=duration_ms,
        status_code=upstream.status_code,
        usage_unit_type="images",
        usage_unit_count=1,
        billable_sku=public_model.billable_sku or display_model,
        upstream_request_id=upstream_request_id,
        image_count=1,
        price_per_image_cents=price_per_image_cents,
        **_channel_usage_kwargs(used_cfg, channel if "channel" in locals() else None),
        **pricing_kwargs,
    )
    await _mark_job_completed(
        job_id,
        result_payload=payload if isinstance(payload, dict) else {"raw": payload},
        upstream_request_id=upstream_request_id,
        duration_ms=duration_ms,
        cost_cents=round(price_per_image_cents),
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
            async with SessionLocal() as session:
                job = await session.get(ImageJob, job_id)
                endpoint = str(job.endpoint or "") if job else ""
            if endpoint == "images/generations":
                await _process_image_generation_job(job_id)
            else:
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
        station_model = await resolve_station_model_for_user(db, user, requested_model, "images/edits")
        resolved = station_model.resolved_model if station_model else model_registry.resolve_public_model(
            requested_model,
            "images/edits",
        )
    except Exception as exc:
        return _model_resolution_error_response(exc)

    (
        resolved,
        station_model,
        _routing_override,
        _user_cache_read_multiplier_override,
        _effective_provider_model,
    ) = apply_user_overrides_to_resolution(user, resolved, station_model)

    public_model = resolved.public_model
    display_model = station_model.display_model if station_model else public_model.public_id
    used_cfg = resolved.backend
    used_route_reason = _bounded_route_reason(resolved.route_reason)
    price_per_image_cents = station_model.retail_price_per_image_cents if station_model else public_model.price_per_image_cents
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
        requested_model=requested_model or display_model,
        form_fields=form_fields,
        file_fields=file_fields,
    )
    manifest["coincoin_snapshot"] = {
        "display_model": display_model,
        "resolved_public_model": public_model.public_id,
        "retail_price_per_image_cents": float(price_per_image_cents or 0.0),
        "public_pricing": public_model_pricing_kwargs(public_model),
        "station_usage": station_usage_kwargs(station_model),
    }
    for idx, (_, (_, content, _)) in enumerate(file_fields):
        stored_name = manifest["files"][idx]["stored_name"]
        (job_dir / stored_name).write_bytes(content)

    job = ImageJob(
        id=job_id,
        user_id=user.id,
        api_key_id=getattr(user, _KEY_ID_ATTR, "") or None,
        status=JOB_STATUS_QUEUED,
        endpoint="images/edits",
        public_model=display_model,
        provider_model=str(used_cfg.model_id or _effective_provider_model or public_model.provider_model or "").strip(),
        route_reason=used_route_reason,
        channel_id=used_cfg.channel_id,
        channel_type=used_cfg.channel_type,
        provider_platform=used_cfg.provider_platform,
        provider_account_fingerprint=used_cfg.provider_account_fingerprint,
        image_count=image_count,
        request_payload_json=json.dumps(manifest, ensure_ascii=False),
        storage_dir=str(job_dir),
    )
    db.add(job)
    try:
        await db.commit()
        await db.refresh(job)
    except Exception:
        logger.exception("failed to create image edit job")
        await db.rollback()
        _cleanup_job_storage(str(job_dir))
        return _openai_error_response(
            "Unable to create image edit job.",
            code="image_job_create_failed",
            error_type="server_error",
            status_code=500,
        )
    return JSONResponse(status_code=202, content=_job_response(job))


async def _create_image_generation_job(request: Request, db: AsyncSession) -> JSONResponse:
    if not settings.image_jobs_enabled:
        return _openai_error_response(
            "Async image jobs are disabled on this deployment.",
            code="image_jobs_disabled",
            error_type="server_error",
            status_code=503,
        )

    user = await authorize_workbench_request(request, db)
    try:
        payload = await request.json()
    except Exception:
        return _openai_error_response("Invalid JSON payload.", code="invalid_json_payload", status_code=400)
    if not isinstance(payload, dict):
        return _openai_error_response("Payload must be a JSON object.", code="invalid_json_payload", status_code=400)

    requested_model = str(payload.get("model") or "").strip()
    try:
        station_model = await resolve_station_model_for_user(db, user, requested_model, "images/generations")
        resolved = station_model.resolved_model if station_model else model_registry.resolve_public_model(
            requested_model,
            "images/generations",
        )
    except Exception as exc:
        return _model_resolution_error_response(exc)

    (
        resolved,
        station_model,
        _routing_override,
        _user_cache_read_multiplier_override,
        _effective_provider_model,
    ) = apply_user_overrides_to_resolution(user, resolved, station_model)

    public_model = resolved.public_model
    display_model = station_model.display_model if station_model else public_model.public_id
    used_cfg = resolved.backend
    used_route_reason = _bounded_route_reason(resolved.route_reason)
    price_per_image_cents = station_model.retail_price_per_image_cents if station_model else public_model.price_per_image_cents
    is_google_image_generation = public_model.provider_name.strip().lower() == "google"
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    if is_google_image_generation and delivery_lane not in {"gateway", gemini_cpa.DELIVERY_LANE, "vertex_direct"}:
        return _unsupported_google_image_lane_error(delivery_lane)
    if is_google_image_generation and _requested_image_count_from_json(payload) > 1:
        return _vertex_image_candidate_count_error()
    if delivery_lane == "vertex_direct" and not settings.vertex_api_key:
        return _openai_error_response(
            "Gemini image generation requires COINCOIN_VERTEX_API_KEY on the CoinCoin control plane.",
            error_type="server_error",
            code="vertex_image_generation_not_configured",
            status_code=503,
        )

    image_count = _requested_image_count_from_json(payload)
    manifest = {
        "requested_model": requested_model or display_model,
        "payload": payload,
        "coincoin_snapshot": {
            "display_model": display_model,
            "resolved_public_model": public_model.public_id,
            "retail_price_per_image_cents": float(price_per_image_cents or 0.0),
            "public_pricing": public_model_pricing_kwargs(public_model),
            "station_usage": station_usage_kwargs(station_model),
        },
    }
    job = ImageJob(
        id=secrets.token_hex(16),
        user_id=user.id,
        api_key_id=getattr(user, _KEY_ID_ATTR, "") or None,
        status=JOB_STATUS_QUEUED,
        endpoint="images/generations",
        public_model=display_model,
        provider_model=str(used_cfg.model_id or _effective_provider_model or public_model.provider_model or "").strip(),
        route_reason=used_route_reason,
        channel_id=used_cfg.channel_id,
        channel_type=used_cfg.channel_type,
        provider_platform=used_cfg.provider_platform,
        provider_account_fingerprint=used_cfg.provider_account_fingerprint,
        image_count=image_count,
        request_payload_json=json.dumps(manifest, ensure_ascii=False),
        storage_dir="",
    )
    db.add(job)
    try:
        await db.commit()
        await db.refresh(job)
    except Exception:
        logger.exception("failed to create image generation job")
        await db.rollback()
        return _openai_error_response(
            "Unable to create image generation job.",
            code="image_job_create_failed",
            error_type="server_error",
            status_code=500,
        )
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
    return await _run_image_job_handler(_create_image_edit_job, request, db)


@openai_router.post("/image-jobs/edits")
async def create_image_edit_job_openai(request: Request, db: AsyncSession = Depends(get_db)):
    return await _run_image_job_handler(_create_image_edit_job, request, db)


@router.post("/image-jobs/generations")
async def create_image_generation_job(request: Request, db: AsyncSession = Depends(get_db)):
    return await _run_image_job_handler(_create_image_generation_job, request, db)


@openai_router.post("/image-jobs/generations")
async def create_image_generation_job_openai(request: Request, db: AsyncSession = Depends(get_db)):
    return await _run_image_job_handler(_create_image_generation_job, request, db)


@router.get("/image-jobs/{job_id}")
async def get_image_job(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _get_image_job(job_id, request, db)


@openai_router.get("/image-jobs/{job_id}")
async def get_image_job_openai(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _get_image_job(job_id, request, db)
