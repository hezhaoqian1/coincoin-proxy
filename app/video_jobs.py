from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .billing import (
    add_billing_ledger,
    BillingError,
    available_subscription_cents,
    debit_usage_cents,
    get_available_balance_cents,
    get_traffic_pack_for_update,
)
from .channel_router import channel_router
from .db import get_db
from .finance_summary import increment_finance_summary
from .media_store import record_media_artifacts_best_effort
from .models import RequestLog, UsageDaily, User, UserSubscription, VideoJob
from .proxy import (
    _KEY_ID_ATTR,
    _build_upstream_headers,
    _channel_usage_kwargs,
    _model_resolution_error_response,
    _openai_error_response,
    _record_channel_failure,
    _record_channel_success,
    authenticate_user,
    authorize_request,
    extract_upstream_request_id,
    get_http_client,
)
from .router import ModelConfig, registry as model_registry
from .security import generate_id
from .config import settings
from .station_runtime import public_model_pricing_kwargs
from .usage_buffer import china_today, usage_buffer
from .referral import process_first_usage_referral_reward


logger = logging.getLogger("coincoin.video_jobs")

router = APIRouter(prefix="/openai/v1", tags=["video-jobs"])
openai_router = APIRouter(prefix="/v1", tags=["video-jobs"])

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
TERMINAL_STATUSES = {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED}
SEEDANCE_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}
SEEDANCE_VIDEO_MODELS = {
    "seedance-v2-720p-video",
    "seedance-v2-1080p-video",
}
SEEDANCE_IMAGE_ONLY_MODELS = {
    "seedance-v2-720p",
    "seedance-v2-1080p",
}
BLOCKED_PARAM_KEYS = {"duration", "durations", "seconds", "second", "resolution", "size"}


def _video_job_response(job: VideoJob) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": job.id,
        "object": "video.generation",
        "task_id": job.id,
        "upstream_task_id": job.upstream_task_id,
        "status": job.status,
        "endpoint": job.endpoint,
        "model": job.public_model,
        "provider_model": job.provider_model,
        "attempt_count": int(job.attempt_count or 0),
        "charged_cents": int(job.charged_cents or 0),
        "refunded_cents": int(job.refunded_cents or 0),
        "route_reason": job.route_reason,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
    result = _json_loads(job.result_payload_json)
    if isinstance(result, dict):
        output = _extract_output(result)
        if output:
            payload["output"] = output
        payload["result"] = result
    if job.status == JOB_STATUS_FAILED:
        payload["error"] = {
            "code": job.error_code or "video_job_failed",
            "message": job.error_message or "Video generation failed",
        }
    return payload


def _json_loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def _extract_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    candidates = []
    if isinstance(data, dict):
        candidates.extend([data.get("output"), data])
    candidates.extend([payload.get("output"), payload])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("url") or candidate.get("video_url")
        if isinstance(url, str) and url.strip():
            return {"type": "video", "url": url.strip()}
    return {}


def _seedance_task_url(base_url: str, path: str) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(cleaned)
    current_path = parsed.path.rstrip("/")
    suffix = f"/{path.lstrip('/')}"
    if current_path.endswith("/v1"):
        next_path = f"{current_path}{suffix}"
    else:
        next_path = f"{current_path}/v1{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, next_path, parsed.query, parsed.fragment))


def _content_url(item: dict, key: str) -> str:
    value = item.get(key)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("url") or "").strip()
    return ""


def _validate_seedance_payload(payload: dict) -> Optional[JSONResponse]:
    model = str(payload.get("model") or getattr(model_registry, "default_video_model_id", "") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return _openai_error_response("Video generation requires a prompt.", code="missing_prompt", param="prompt", status_code=400)

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return _openai_error_response("params must be a JSON object.", code="invalid_params", param="params", status_code=400)

    blocked = sorted(key for key in BLOCKED_PARAM_KEYS if key in params)
    if blocked:
        return _openai_error_response(
            "Seedance output duration and resolution are controlled by the model name.",
            code="unsupported_video_param",
            param=f"params.{blocked[0]}",
            status_code=400,
        )

    ratio = str(params.get("ratio") or "16:9").strip()
    if ratio not in SEEDANCE_RATIOS:
        return _openai_error_response(
            "params.ratio must be one of: 16:9, 4:3, 1:1, 3:4, 9:16, 21:9, adaptive.",
            code="invalid_ratio",
            param="params.ratio",
            status_code=400,
        )

    simple_images = params.get("images") or []
    if isinstance(simple_images, str):
        simple_images = [simple_images]
    if not isinstance(simple_images, list):
        return _openai_error_response("params.images must be an array of image URLs.", code="invalid_images", param="params.images", status_code=400)
    simple_image_count = sum(1 for item in simple_images if str(item or "").strip())

    content = params.get("content") or []
    if not isinstance(content, list):
        return _openai_error_response("params.content must be an array.", code="invalid_content", param="params.content", status_code=400)

    has_first_frame = False
    has_last_frame = False
    has_reference_image = simple_image_count > 0
    has_video_reference = False
    has_audio_reference = False
    media_reference_count = simple_image_count
    reference_image_count = simple_image_count

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        role = str(item.get("role") or "").strip()
        if item_type == "image_url" and _content_url(item, "image_url"):
            media_reference_count += 1
            if role == "first_frame":
                has_first_frame = True
            elif role == "last_frame":
                has_last_frame = True
            else:
                has_reference_image = True
                reference_image_count += 1
        elif item_type == "video_url" and _content_url(item, "video_url"):
            media_reference_count += 1
            has_video_reference = True
        elif item_type == "audio_url" and _content_url(item, "audio_url"):
            media_reference_count += 1
            has_audio_reference = True
        elif role == "reference_video":
            has_video_reference = True
        elif role == "reference_audio":
            has_audio_reference = True

    if params.get("reference_video"):
        has_video_reference = True
        media_reference_count += 1
    if params.get("reference_audio"):
        has_audio_reference = True
        media_reference_count += 1

    if media_reference_count <= 0:
        return _openai_error_response(
            "Seedance 2.0 requires at least one image, keyframe, video, or audio reference.",
            code="missing_reference_media",
            param="params",
            status_code=400,
        )
    if has_video_reference or has_audio_reference:
        if model not in SEEDANCE_VIDEO_MODELS:
            return _openai_error_response(
                "Video or audio references require a -video Seedance model.",
                code="video_reference_requires_video_model",
                param="model",
                status_code=400,
            )
    if (has_first_frame or has_last_frame) and has_reference_image:
        return _openai_error_response(
            "first_frame/last_frame keyframes cannot be mixed with reference_image inputs.",
            code="mixed_image_roles",
            param="params.content",
            status_code=400,
        )
    if has_last_frame and not has_first_frame:
        return _openai_error_response(
            "last_frame requires a matching first_frame.",
            code="last_frame_without_first_frame",
            param="params.content",
            status_code=400,
        )
    if reference_image_count > 9:
        return _openai_error_response(
            "Seedance reference_image mode supports at most 9 image references.",
            code="reference_image_limit_exceeded",
            param="params.content",
            status_code=400,
        )
    return None


def _normalize_upstream_status(raw: Any) -> str:
    status = str(raw or "").strip().lower()
    if status in {"completed", "complete", "success", "succeeded", "done"}:
        return JOB_STATUS_COMPLETED
    if status in {"failed", "failure", "error", "cancelled", "canceled"}:
        return JOB_STATUS_FAILED
    if status in {"running", "processing", "in_progress", "progress"}:
        return JOB_STATUS_RUNNING
    return JOB_STATUS_QUEUED


def _extract_task_id(payload: Dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("task_id", "id"):
            value = data.get(key)
            if value:
                return str(value).strip()
    for key in ("task_id", "id"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return ""


def _extract_status(payload: Dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("status", "state"):
            if data.get(key) is not None:
                return _normalize_upstream_status(data.get(key))
    for key in ("status", "state"):
        if payload.get(key) is not None:
            return _normalize_upstream_status(payload.get(key))
    if _extract_output(payload):
        return JOB_STATUS_COMPLETED
    return JOB_STATUS_QUEUED


def _extract_error(payload: Dict[str, Any]) -> tuple[str, str]:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "upstream_error"), str(error.get("message") or error)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("error", "error_message", "message", "fail_reason"):
            if data.get(key):
                return "upstream_error", str(data.get(key))
    if payload.get("message"):
        return str(payload.get("code") or "upstream_error"), str(payload.get("message"))
    return "video_job_failed", "Video generation failed."


async def _read_json_response(response: httpx.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads(response.text)
        except Exception:
            payload = {}
    return payload if isinstance(payload, dict) else {"raw": payload}


def _backend_for_job(job: VideoJob) -> Optional[ModelConfig]:
    try:
        resolved = model_registry.resolve_public_model(job.public_model, "videos/generations")
    except Exception:
        return None

    channel_id = str(job.channel_id or "").strip()
    if channel_id and not channel_id.startswith("system:"):
        for channel in channel_router.list_channels():
            if channel.channel_id != channel_id:
                continue
            if not (channel.base_url and channel.api_key):
                break
            return ModelConfig(
                model_id=job.provider_model or resolved.backend.model_id,
                upstream_url=channel.base_url,
                api_key=channel.api_key,
                price_input_per_million=0,
                price_output_per_million=0,
                strip_unsupported=False,
                auth_style=channel.auth_style or resolved.backend.auth_style,
                channel_id=channel.channel_id,
                channel_type=channel.channel_type,
                provider_platform=channel.provider_platform,
                provider_account_fingerprint=channel.provider_account_fingerprint,
            )
    return resolved.backend


def _video_job_debit_total(job: VideoJob) -> int:
    return (
        int(getattr(job, "subscription_debit_cents", 0) or 0)
        + int(getattr(job, "traffic_pack_debit_cents", 0) or 0)
        + int(getattr(job, "legacy_debit_cents", 0) or 0)
    )


def _traffic_pack_debits_for_job(job: VideoJob) -> list[dict[str, Any]]:
    raw = _json_loads(getattr(job, "traffic_pack_debits_json", None))
    if not isinstance(raw, list):
        return []
    debits: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cents = max(0, int(item.get("cents") or 0))
        pack_id = str(item.get("id") or "").strip()
        if cents <= 0 or not pack_id:
            continue
        debits.append({
            "id": pack_id,
            "product_id": str(item.get("product_id") or "").strip(),
            "cents": cents,
        })
    return debits


async def _record_video_usage_daily(
    db: AsyncSession,
    user_id: str,
    *,
    videos_total: int,
    requests_total: int,
    cost_cents: int,
) -> None:
    stmt = mysql_insert(UsageDaily).values(
        user_id=user_id,
        day=china_today(),
        tokens_total=0,
        input_tokens=0,
        output_tokens=0,
        images_total=0,
        videos_total=int(videos_total or 0),
        cost_cents=int(cost_cents or 0),
        requests_total=int(requests_total or 0),
        updated_at=datetime.utcnow(),
    )
    stmt = stmt.on_duplicate_key_update(
        videos_total=UsageDaily.videos_total + int(videos_total or 0),
        cost_cents=UsageDaily.cost_cents + int(cost_cents or 0),
        requests_total=UsageDaily.requests_total + int(requests_total or 0),
        updated_at=datetime.utcnow(),
    )
    await db.execute(stmt)


def _create_video_request_log(
    *,
    user_id: str,
    api_key_id: str | None,
    public_model,
    used_cfg: ModelConfig,
    route_reason: str,
    duration_ms: int,
    status_code: int,
    upstream_request_id: str,
    cost_cents: int,
) -> RequestLog:
    pricing = public_model_pricing_kwargs(public_model)
    channel = _channel_usage_kwargs(used_cfg)
    return RequestLog(
        id=generate_id("rl_"),
        user_id=user_id,
        api_key_id=api_key_id,
        endpoint="videos/generations",
        model=public_model.public_id,
        provider_model=used_cfg.model_id or public_model.provider_model,
        customer_model_alias=public_model.public_id,
        usage_unit_type="videos",
        usage_unit_count=1,
        video_count=1,
        billable_sku=public_model.billable_sku or public_model.public_id,
        upstream_request_id=upstream_request_id,
        channel_id=channel.get("channel_id", ""),
        channel_type=channel.get("channel_type", ""),
        provider_platform=channel.get("provider_platform", ""),
        provider_account_fingerprint=channel.get("provider_account_fingerprint", ""),
        fallback_from_channel_id=channel.get("fallback_from_channel_id", ""),
        route_attempt=channel.get("route_attempt", 0),
        cost_cents=int(cost_cents or 0),
        retail_charge_cents=int(cost_cents or 0),
        duration_ms=duration_ms,
        status_code=status_code,
        route_reason=route_reason,
        price_per_video_cents=float(public_model.price_per_video_cents or 0.0),
        **pricing,
    )


async def _record_video_creation_usage(
    *,
    db: AsyncSession,
    user_id: str,
    api_key_id: str | None,
    public_model,
    used_cfg: ModelConfig,
    route_reason: str,
    duration_ms: int,
    status_code: int,
    upstream_request_id: str,
    cost_cents: int,
) -> None:
    db.add(
        _create_video_request_log(
            user_id=user_id,
            api_key_id=api_key_id,
            public_model=public_model,
            used_cfg=used_cfg,
            route_reason=route_reason,
            duration_ms=duration_ms,
            status_code=status_code,
            upstream_request_id=upstream_request_id,
            cost_cents=cost_cents,
        )
    )
    await _record_video_usage_daily(
        db,
        user_id,
        videos_total=1,
        requests_total=1,
        cost_cents=cost_cents,
    )
    if cost_cents > 0:
        await increment_finance_summary(db, user_id, consumed_cents=cost_cents)
        await process_first_usage_referral_reward(user_id, db)


async def _record_completed_video_artifact(
    db: AsyncSession,
    job: VideoJob,
    payload: Dict[str, Any],
) -> None:
    await record_media_artifacts_best_effort(
        db,
        user_id=job.user_id,
        api_key_id=job.api_key_id,
        media_type="video",
        endpoint="videos/generations",
        model=job.public_model,
        provider_model=job.provider_model,
        payload=payload,
        status=JOB_STATUS_COMPLETED,
        source_type="video_job",
        source_id=job.id,
        upstream_request_id=job.upstream_request_id,
        route_reason=job.route_reason,
        cost_cents=int(job.charged_cents or 0),
        completed_at=job.completed_at or datetime.utcnow(),
    )


async def _charge_video_job_once(
    *,
    db: AsyncSession,
    user: User,
    job_id: str,
    charged_cents: int,
    pending_cost_cents: int,
) -> dict[str, Any]:
    if settings.billing_mode != "balance" or charged_cents <= 0:
        return {
            "subscription_cents": 0,
            "subscription_id": "",
            "subscription_plan_id": "",
            "traffic_pack_cents": 0,
            "traffic_pack_debits": [],
            "legacy_cents": 0,
        }
    locked_user = (
        await db.execute(select(User).where(User.id == user.id).with_for_update())
    ).scalar_one_or_none()
    if not locked_user:
        raise BillingError("user not found", status_code=404)
    return await debit_usage_cents(
        db=db,
        user=locked_user,
        cost_cents=charged_cents,
        source_type="video_job",
        source_id=job_id,
        allow_negative_legacy=False,
        reserved_cents=pending_cost_cents,
    )


async def _refund_failed_job_once(job: VideoJob, db: AsyncSession) -> None:
    if int(job.refunded_cents or 0) > 0:
        return
    amount = _video_job_debit_total(job)
    if amount <= 0:
        return
    user = (
        await db.execute(select(User).where(User.id == job.user_id).with_for_update())
    ).scalar_one_or_none()
    if not user:
        return

    refunded = 0
    subscription_cents = int(getattr(job, "subscription_debit_cents", 0) or 0)
    if subscription_cents > 0 and str(getattr(job, "subscription_id", "") or "").strip():
        sub = (
            await db.execute(
                select(UserSubscription)
                .where(UserSubscription.id == job.subscription_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sub:
            sub.used_cents = max(0, int(sub.used_cents or 0) - subscription_cents)
            refunded += subscription_cents
            add_billing_ledger(
                db,
                user_id=job.user_id,
                entry_type="usage_subscription_refund",
                amount_cents=subscription_cents,
                source_type="video_job",
                source_id=job.id,
                product_id=job.subscription_plan_id,
                balance_after_cents=available_subscription_cents(sub),
                note=f"Seedance task failed: {job.upstream_task_id or job.id}",
            )

    for debit in _traffic_pack_debits_for_job(job):
        pack = await get_traffic_pack_for_update(db, debit["id"])
        if not pack:
            continue
        cents = int(debit["cents"] or 0)
        pack.remaining_cents = int(pack.remaining_cents or 0) + cents
        if str(getattr(pack, "status", "") or "").strip() == "depleted" and pack.remaining_cents > 0:
            pack.status = "active"
        refunded += cents
        add_billing_ledger(
            db,
            user_id=job.user_id,
            entry_type="usage_traffic_pack_refund",
            amount_cents=cents,
            source_type="video_job",
            source_id=job.id,
            product_id=debit.get("product_id", ""),
            balance_after_cents=int(pack.remaining_cents or 0),
            note=f"Seedance task failed: {job.upstream_task_id or job.id}",
        )

    legacy_cents = int(getattr(job, "legacy_debit_cents", 0) or 0)
    if legacy_cents > 0:
        user.balance = int(user.balance or 0) + legacy_cents
        refunded += legacy_cents
        add_billing_ledger(
            db,
            user_id=job.user_id,
            entry_type="usage_legacy_balance_refund",
            amount_cents=legacy_cents,
            source_type="video_job",
            source_id=job.id,
            balance_after_cents=int(user.balance or 0),
            note=f"Seedance task failed: {job.upstream_task_id or job.id}",
        )

    if refunded <= 0:
        return
    job.refunded_cents = refunded
    db.add(
        RequestLog(
            id=generate_id("rl_"),
            user_id=job.user_id,
            api_key_id=job.api_key_id,
            endpoint="videos/generations",
            model=job.public_model,
            provider_model=job.provider_model,
            customer_model_alias=job.public_model,
            usage_unit_type="videos",
            usage_unit_count=-1,
            video_count=-1,
            billable_sku=job.public_model,
            upstream_request_id=job.upstream_request_id,
            channel_id=job.channel_id,
            channel_type=job.channel_type,
            provider_platform=job.provider_platform,
            provider_account_fingerprint=job.provider_account_fingerprint,
            cost_cents=-refunded,
            retail_charge_cents=-refunded,
            status_code=200,
            route_reason="video_job_refund",
            created_at=datetime.utcnow(),
        )
    )
    await _record_video_usage_daily(
        db,
        job.user_id,
        videos_total=-1,
        requests_total=0,
        cost_cents=-refunded,
    )
    await increment_finance_summary(db, job.user_id, consumed_cents=-refunded)


async def _create_video_generation(request: Request, db: AsyncSession) -> JSONResponse:
    user = await authorize_request(request, db)
    try:
        payload = await request.json()
    except Exception:
        return _openai_error_response("Invalid JSON payload.", code="invalid_json", status_code=400)
    if not isinstance(payload, dict):
        return _openai_error_response("Payload must be a JSON object.", code="invalid_payload", status_code=400)

    validation_error = _validate_seedance_payload(payload)
    if validation_error is not None:
        return validation_error

    requested_model = str(payload.get("model") or "").strip()
    try:
        resolved = model_registry.resolve_public_model(requested_model, "videos/generations")
    except Exception as exc:
        return _model_resolution_error_response(exc)

    public_model = resolved.public_model
    used_cfg = resolved.backend
    charged_cents = round(float(public_model.price_per_video_cents or 0.0))
    pending_cost = 0
    if settings.billing_mode == "balance" and charged_cents > 0:
        pending_cost = await usage_buffer.get_pending_cost(user.id)
        available = await get_available_balance_cents(db, user, pending_cost_cents=pending_cost)
        if int(available.get("available_cents", 0)) < charged_cents:
            return _openai_error_response(
                "Insufficient balance for this video generation task.",
                error_type="billing_error",
                code="insufficient_balance",
                status_code=402,
            )

    upstream_payload = {
        "model": used_cfg.model_id or public_model.upstream_model or public_model.public_id,
        "prompt": str(payload.get("prompt") or ""),
        "params": payload.get("params") or {},
    }
    started = time.monotonic()
    try:
        client = await get_http_client()
        upstream = await client.post(
            _seedance_task_url(used_cfg.upstream_url, "task/create"),
            json=upstream_payload,
            headers=_build_upstream_headers(used_cfg),
        )
    except Exception as exc:
        _record_channel_failure(used_cfg, error_code="upstream_transport_error")
        return _openai_error_response(
            f"Seedance upstream is unreachable: {exc}",
            error_type="server_error",
            code="upstream_transport_error",
            status_code=502,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    upstream_request_id = extract_upstream_request_id(upstream.headers)
    upstream_json = await _read_json_response(upstream)
    if upstream.status_code >= 400:
        _record_channel_failure(used_cfg, status_code=upstream.status_code)
        code, message = _extract_error(upstream_json)
        return _openai_error_response(message, error_type="server_error", code=code, status_code=upstream.status_code)

    upstream_code = upstream_json.get("code")
    if upstream_code not in (None, 0, "0"):
        _record_channel_failure(used_cfg, error_code=str(upstream_code or "upstream_error"))
        code, message = _extract_error(upstream_json)
        return _openai_error_response(message, error_type="server_error", code=code, status_code=502)

    upstream_task_id = _extract_task_id(upstream_json)
    if not upstream_task_id:
        _record_channel_failure(used_cfg, error_code="missing_task_id")
        return _openai_error_response(
            "Seedance upstream did not return a task id.",
            error_type="server_error",
            code="missing_task_id",
            status_code=502,
        )

    status = _extract_status(upstream_json)
    if status == JOB_STATUS_FAILED:
        code, message = _extract_error(upstream_json)
    else:
        code, message = "", ""

    _record_channel_success(used_cfg, duration_ms=duration_ms)
    job_id = secrets.token_hex(16)
    api_key_id = getattr(user, _KEY_ID_ATTR, "") or None
    try:
        debit_result = await _charge_video_job_once(
            db=db,
            user=user,
            job_id=job_id,
            charged_cents=charged_cents,
            pending_cost_cents=pending_cost,
        )
    except BillingError as exc:
        return _openai_error_response(
            exc.detail,
            error_type="billing_error",
            code="insufficient_balance",
            status_code=exc.status_code,
        )

    job = VideoJob(
        id=job_id,
        user_id=user.id,
        api_key_id=api_key_id,
        status=status,
        endpoint="videos/generations",
        public_model=public_model.public_id,
        provider_model=used_cfg.model_id or public_model.provider_model,
        route_reason=resolved.route_reason,
        upstream_task_id=upstream_task_id,
        request_payload_json=json.dumps(upstream_payload, ensure_ascii=False),
        result_payload_json=json.dumps(upstream_json, ensure_ascii=False),
        error_code=code,
        error_message=message,
        upstream_request_id=upstream_request_id,
        channel_id=used_cfg.channel_id,
        channel_type=used_cfg.channel_type,
        provider_platform=used_cfg.provider_platform,
        provider_account_fingerprint=used_cfg.provider_account_fingerprint,
        charged_cents=charged_cents,
        subscription_debit_cents=int(debit_result.get("subscription_cents", 0) or 0),
        subscription_id=str(debit_result.get("subscription_id", "") or ""),
        subscription_plan_id=str(debit_result.get("subscription_plan_id", "") or ""),
        traffic_pack_debit_cents=int(debit_result.get("traffic_pack_cents", 0) or 0),
        traffic_pack_debits_json=json.dumps(debit_result.get("traffic_pack_debits") or [], ensure_ascii=False),
        legacy_debit_cents=int(debit_result.get("legacy_cents", 0) or 0),
        duration_ms=duration_ms,
        started_at=datetime.utcnow(),
        completed_at=datetime.utcnow() if status in TERMINAL_STATUSES else None,
    )
    db.add(job)
    await _record_video_creation_usage(
        db=db,
        user_id=user.id,
        api_key_id=api_key_id,
        public_model=public_model,
        used_cfg=used_cfg,
        route_reason=resolved.route_reason,
        duration_ms=duration_ms,
        status_code=upstream.status_code,
        upstream_request_id=upstream_request_id,
        cost_cents=charged_cents,
    )

    if job.status == JOB_STATUS_FAILED:
        await _refund_failed_job_once(job, db)
    elif job.status == JOB_STATUS_COMPLETED:
        await _record_completed_video_artifact(db, job, upstream_json)
    await db.commit()
    await db.refresh(job)
    return JSONResponse(status_code=202, content=_video_job_response(job))


async def _refresh_video_job(job: VideoJob, db: AsyncSession) -> None:
    if job.status in TERMINAL_STATUSES:
        return
    used_cfg = _backend_for_job(job)
    if used_cfg is None:
        job.status = JOB_STATUS_FAILED
        job.error_code = "model_resolution_failed"
        job.error_message = "Unable to resolve the original video generation model."
        job.completed_at = datetime.utcnow()
        await _refund_failed_job_once(job, db)
        return

    started = time.monotonic()
    job.attempt_count = int(job.attempt_count or 0) + 1
    try:
        client = await get_http_client()
        upstream = await client.post(
            _seedance_task_url(used_cfg.upstream_url, "task/query"),
            json={"task_id": job.upstream_task_id},
            headers=_build_upstream_headers(used_cfg),
        )
    except Exception as exc:
        _record_channel_failure(used_cfg, error_code="upstream_transport_error")
        job.error_code = "upstream_transport_error"
        job.error_message = str(exc)
        return

    job.duration_ms = int((time.monotonic() - started) * 1000)
    if extract_upstream_request_id(upstream.headers):
        job.upstream_request_id = extract_upstream_request_id(upstream.headers)
    payload = await _read_json_response(upstream)
    if upstream.status_code >= 400:
        _record_channel_failure(used_cfg, status_code=upstream.status_code)
        job.error_code, job.error_message = _extract_error(payload)
        return

    upstream_code = payload.get("code")
    if upstream_code not in (None, 0, "0"):
        _record_channel_failure(used_cfg, error_code=str(upstream_code or "upstream_error"))
        job.status = JOB_STATUS_FAILED
        job.error_code, job.error_message = _extract_error(payload)
        job.completed_at = datetime.utcnow()
        job.result_payload_json = json.dumps(payload, ensure_ascii=False)
        await _refund_failed_job_once(job, db)
        return

    _record_channel_success(used_cfg, duration_ms=job.duration_ms)
    job.status = _extract_status(payload)
    job.result_payload_json = json.dumps(payload, ensure_ascii=False)
    if job.status == JOB_STATUS_FAILED:
        job.error_code, job.error_message = _extract_error(payload)
        job.completed_at = datetime.utcnow()
        await _refund_failed_job_once(job, db)
    elif job.status == JOB_STATUS_COMPLETED:
        job.error_code = ""
        job.error_message = ""
        job.completed_at = datetime.utcnow()
        await _record_completed_video_artifact(db, job, payload)


async def _get_video_generation(job_id: str, request: Request, db: AsyncSession) -> JSONResponse:
    user = await authenticate_user(request, db)
    result = await db.execute(
        select(VideoJob).where(
            VideoJob.user_id == user.id,
            or_(VideoJob.id == job_id, VideoJob.upstream_task_id == job_id),
        )
    )
    job = result.scalar_one_or_none()
    if not job:
        return _openai_error_response("Video job not found.", code="video_job_not_found", status_code=404)
    await _refresh_video_job(job, db)
    await db.commit()
    await db.refresh(job)
    return JSONResponse(content=_video_job_response(job))


@router.post("/videos/generations")
async def create_video_generation_compat(request: Request, db: AsyncSession = Depends(get_db)):
    return await _create_video_generation(request, db)


@openai_router.post("/videos/generations")
async def create_video_generation(request: Request, db: AsyncSession = Depends(get_db)):
    return await _create_video_generation(request, db)


@router.get("/videos/generations/{job_id}")
async def get_video_generation_compat(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _get_video_generation(job_id, request, db)


@openai_router.get("/videos/generations/{job_id}")
async def get_video_generation(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _get_video_generation(job_id, request, db)
