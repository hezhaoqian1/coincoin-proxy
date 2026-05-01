import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import settings
from .router import registry as model_registry
from .security import require_admin


ops_router = APIRouter(prefix="/ops/monitoring", tags=["monitoring"])
admin_router = APIRouter(prefix="/admin/monitoring", tags=["admin-monitoring"])

_MARKER = "COINCOIN_MONITOR_OK"


class MonitoringConfigError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_monitoring_token(request: Request) -> Optional[str]:
    token = request.headers.get("x-monitoring-token") or request.headers.get(
        "authorization"
    )
    if token and token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    return token.strip() if token else None


def require_monitoring(request: Request) -> None:
    configured = (settings.monitoring_token or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="monitoring token is not configured",
        )

    presented = _resolve_monitoring_token(request)
    if presented != configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )


def _require_public_probe_config() -> Dict[str, str]:
    public_base_url = (
        getattr(settings, "monitoring_public_base_url", "") or settings.self_base_url
    ).strip()
    api_key = (getattr(settings, "monitoring_api_key", "") or "").strip()

    if not public_base_url:
        raise MonitoringConfigError(
            "missing monitoring_public_base_url (or COINCOIN_SELF_BASE_URL)"
        )
    if not api_key:
        raise MonitoringConfigError("missing monitoring_api_key")

    return {"public_base_url": public_base_url.rstrip("/"), "api_key": api_key}


def _get_timeout_seconds() -> float:
    raw = getattr(settings, "monitoring_timeout_seconds", 45) or 45
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = 45.0
    return max(5.0, min(timeout, 180.0))


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _monitoring_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "coincoin-monitoring/1.0",
    }


def _default_text_model_for(capability: str) -> str:
    for model in model_registry.list_public_models(capability):
        if capability in model.capabilities:
            return model.public_id
    if capability == "responses" and model_registry.default_text_model_id:
        return model_registry.default_text_model_id
    return ""


def _resolve_probe_model(setting_name: str, capability: str) -> str:
    configured = str(getattr(settings, setting_name, "") or "").strip()
    if configured:
        return configured
    return _default_text_model_for(capability)


def _extract_chat_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _extract_responses_content(payload: Dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if not isinstance(output, list):
        return ""

    parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        elif isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _probe_result(
    *,
    probe: str,
    ok: bool,
    latency_ms: Optional[int],
    target: str,
    details: Dict[str, Any],
    checked_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "probe": probe,
        "ok": ok,
        "checked_at": checked_at or _utc_now(),
        "latency_ms": latency_ms,
        "target": target,
        "details": details,
    }


async def _request_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    timeout = httpx.Timeout(_get_timeout_seconds())
    async with httpx.AsyncClient(timeout=timeout) as client:
        started_at = time.perf_counter()
        response = await client.request(method, url, headers=headers, json=json_body)
        latency_ms = int((time.perf_counter() - started_at) * 1000)

    try:
        body = response.json()
    except Exception:
        body = {"raw_text": response.text[:1000]}

    return {
        "status_code": response.status_code,
        "body": body,
        "latency_ms": latency_ms,
        "headers": dict(response.headers),
    }


async def _request_stream_first_chunk(
    url: str, *, headers: Dict[str, str], json_body: Dict[str, Any]
) -> Dict[str, Any]:
    timeout = httpx.Timeout(_get_timeout_seconds())
    async with httpx.AsyncClient(timeout=timeout) as client:
        started_at = time.perf_counter()
        async with client.stream("POST", url, headers=headers, json=json_body) as response:
            first_chunk = ""
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    first_chunk = line
                    break
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            body_preview = first_chunk[:500]

    return {
        "status_code": response.status_code,
        "first_chunk": body_preview,
        "latency_ms": latency_ms,
        "headers": dict(response.headers),
    }


def build_monitoring_summary() -> Dict[str, Any]:
    model_registry.ensure_initialized()
    public_base_url = (
        getattr(settings, "monitoring_public_base_url", "") or settings.self_base_url
    ).strip()
    gateway_health_url = (
        getattr(settings, "monitoring_gateway_health_url", "") or ""
    ).strip()
    chat_model = _resolve_probe_model("monitoring_chat_model", "chat/completions")
    responses_model = _resolve_probe_model("monitoring_responses_model", "responses")

    return {
        "ui_scope": "admin_only",
        "user_status_page": False,
        "configured": {
            "monitoring_token": bool((settings.monitoring_token or "").strip()),
            "public_base_url": bool(public_base_url),
            "monitoring_api_key": bool(
                (getattr(settings, "monitoring_api_key", "") or "").strip()
            ),
            "gateway_health_url": bool(gateway_health_url),
        },
        "public_base_url": public_base_url or None,
        "gateway_health_url": gateway_health_url or None,
        "probe_models": {
            "chat_completions": chat_model or None,
            "responses": responses_model or None,
        },
        "recommended_architecture": {
            "frontend": "管理员后台展示监控说明、配置状态、探针结果，不做终端用户状态页。",
            "backend": "由受保护的 ops probes 承担真实探测，Checkly 只调用 probes，不直接耦合内部实现。",
        },
        "checkly": {
            "headers": ["x-monitoring-token: <COINCOIN_MONITORING_TOKEN>"],
            "recommended_checks": [
                {"name": "public-health", "method": "GET", "path": "/ops/monitoring/probes/public-health"},
                {"name": "catalog", "method": "GET", "path": "/ops/monitoring/probes/catalog"},
                {"name": "chat-completions", "method": "POST", "path": "/ops/monitoring/probes/chat-completions"},
                {"name": "chat-stream", "method": "POST", "path": "/ops/monitoring/probes/chat-stream"},
                {"name": "responses", "method": "POST", "path": "/ops/monitoring/probes/responses"},
                {"name": "gateway-readiness", "method": "GET", "path": "/ops/monitoring/probes/gateway-readiness", "optional": True},
            ],
        },
        "catalog": {
            "default_text_model": model_registry.default_text_model_id or None,
            "default_embedding_model": model_registry.default_embedding_model_id or None,
            "default_image_model": model_registry.default_image_model_id or None,
            "public_models": [
                {
                    "id": model.public_id,
                    "provider": model.provider_name,
                    "capabilities": list(model.capabilities),
                    "delivery_lane": model.delivery_lane,
                    "routing_mode": model.routing_mode,
                }
                for model in model_registry.list_public_models()
            ],
        },
    }


async def run_public_health_probe() -> Dict[str, Any]:
    cfg = _require_public_probe_config()
    url = _join_url(cfg["public_base_url"], "/health")
    result = await _request_json("GET", url)
    body = result["body"]
    ok = result["status_code"] == 200 and isinstance(body, dict) and body.get("status") == "ok"
    return _probe_result(
        probe="public-health",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "service": body.get("service") if isinstance(body, dict) else None,
            "body": body if isinstance(body, dict) else None,
        },
    )


async def run_catalog_probe() -> Dict[str, Any]:
    cfg = _require_public_probe_config()
    url = _join_url(cfg["public_base_url"], "/v1/models")
    headers = _monitoring_headers(cfg["api_key"])
    result = await _request_json("GET", url, headers=headers)
    body = result["body"]
    data = body.get("data") if isinstance(body, dict) else None
    chat_model = _resolve_probe_model("monitoring_chat_model", "chat/completions")
    responses_model = _resolve_probe_model("monitoring_responses_model", "responses")
    returned_ids = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                returned_ids.append(str(item["id"]))

    ok = (
        result["status_code"] == 200
        and isinstance(data, list)
        and (not chat_model or chat_model in returned_ids)
        and (not responses_model or responses_model in returned_ids)
    )
    return _probe_result(
        probe="catalog",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "model_count": len(returned_ids),
            "monitoring_chat_model": chat_model or None,
            "monitoring_responses_model": responses_model or None,
            "returned_model_ids": returned_ids[:25],
        },
    )


async def run_chat_completions_probe(stream: bool = False) -> Dict[str, Any]:
    cfg = _require_public_probe_config()
    model = _resolve_probe_model("monitoring_chat_model", "chat/completions")
    if not model:
        raise MonitoringConfigError("missing monitoring chat model")

    url = _join_url(cfg["public_base_url"], "/v1/chat/completions")
    headers = _monitoring_headers(cfg["api_key"])
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 16,
        "stream": stream,
        "messages": [
            {
                "role": "user",
                "content": f"Reply with exactly {_MARKER}. No punctuation.",
            }
        ],
    }

    if stream:
        result = await _request_stream_first_chunk(url, headers=headers, json_body=payload)
        first_chunk = result["first_chunk"]
        ok = result["status_code"] == 200 and "data:" in first_chunk
        return _probe_result(
            probe="chat-stream",
            ok=ok,
            latency_ms=result["latency_ms"],
            target=url,
            details={
                "http_status": result["status_code"],
                "model": model,
                "first_chunk": first_chunk,
            },
        )

    result = await _request_json("POST", url, headers=headers, json_body=payload)
    body = result["body"]
    content = _extract_chat_content(body) if isinstance(body, dict) else ""
    normalized = content.strip().upper()
    ok = result["status_code"] == 200 and _MARKER in normalized
    return _probe_result(
        probe="chat-completions",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "model": model,
            "response_excerpt": content[:200],
        },
    )


async def run_responses_probe() -> Dict[str, Any]:
    cfg = _require_public_probe_config()
    model = _resolve_probe_model("monitoring_responses_model", "responses")
    if not model:
        raise MonitoringConfigError("missing monitoring responses model")

    url = _join_url(cfg["public_base_url"], "/v1/responses")
    headers = _monitoring_headers(cfg["api_key"])
    payload = {
        "model": model,
        "input": f"Reply with exactly {_MARKER}. No punctuation.",
        "max_output_tokens": 16,
    }
    result = await _request_json("POST", url, headers=headers, json_body=payload)
    body = result["body"]
    content = _extract_responses_content(body) if isinstance(body, dict) else ""
    normalized = content.strip().upper()
    ok = result["status_code"] == 200 and _MARKER in normalized
    return _probe_result(
        probe="responses",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "model": model,
            "response_excerpt": content[:200],
        },
    )


async def run_gateway_readiness_probe() -> Dict[str, Any]:
    url = (getattr(settings, "monitoring_gateway_health_url", "") or "").strip()
    if not url:
        raise MonitoringConfigError("missing monitoring_gateway_health_url")

    result = await _request_json("GET", url)
    body = result["body"]
    ok = result["status_code"] == 200
    return _probe_result(
        probe="gateway-readiness",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "body": body if isinstance(body, dict) else None,
        },
    )


async def _execute_probe(probe_name: str) -> Dict[str, Any]:
    if probe_name == "public-health":
        return await run_public_health_probe()
    if probe_name == "catalog":
        return await run_catalog_probe()
    if probe_name == "chat-completions":
        return await run_chat_completions_probe(stream=False)
    if probe_name == "chat-stream":
        return await run_chat_completions_probe(stream=True)
    if probe_name == "responses":
        return await run_responses_probe()
    if probe_name == "gateway-readiness":
        return await run_gateway_readiness_probe()
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown probe")


def _probe_http_status(result: Dict[str, Any]) -> int:
    if result.get("ok"):
        return status.HTTP_200_OK
    details = result.get("details") or {}
    http_status = details.get("http_status")
    if isinstance(http_status, int) and 400 <= http_status <= 599:
        return http_status
    return status.HTTP_502_BAD_GATEWAY


async def _run_probe_response(probe_name: str) -> JSONResponse:
    try:
        result = await _execute_probe(probe_name)
        return JSONResponse(status_code=_probe_http_status(result), content=result)
    except MonitoringConfigError as exc:
        payload = _probe_result(
            probe=probe_name,
            ok=False,
            latency_ms=None,
            target="configuration",
            details={"error": str(exc)},
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload
        )


def _admin_guard(request: Request) -> None:
    require_admin(request)


@ops_router.get("/summary", dependencies=[Depends(require_monitoring)])
async def ops_monitoring_summary() -> Dict[str, Any]:
    return build_monitoring_summary()


@ops_router.get("/probes/{probe_name}", dependencies=[Depends(require_monitoring)])
async def ops_run_probe(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)


@ops_router.post("/probes/{probe_name}", dependencies=[Depends(require_monitoring)])
async def ops_run_probe_post(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)


@admin_router.get("/summary", dependencies=[Depends(_admin_guard)])
async def admin_monitoring_summary() -> Dict[str, Any]:
    return build_monitoring_summary()


@admin_router.get("/probes/{probe_name}", dependencies=[Depends(_admin_guard)])
async def admin_run_probe(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)


@admin_router.post("/probes/{probe_name}", dependencies=[Depends(_admin_guard)])
async def admin_run_probe_post(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)
