import json
import time
import asyncio
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


def _require_cpa_probe_config() -> Dict[str, str]:
    base_url = (getattr(settings, "monitoring_cpa_base_url", "") or "").strip()
    api_key = (getattr(settings, "monitoring_cpa_api_key", "") or "").strip()

    if not base_url:
        raise MonitoringConfigError("missing monitoring_cpa_base_url")
    if not api_key:
        raise MonitoringConfigError("missing monitoring_cpa_api_key")

    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")

    return {"base_url": normalized, "api_key": api_key}


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
    cpa_base_url = (getattr(settings, "monitoring_cpa_base_url", "") or "").strip()
    cpa_chat_model = (
        str(getattr(settings, "monitoring_cpa_chat_model", "") or "").strip()
        or chat_model
    )
    cpa_responses_model = (
        str(getattr(settings, "monitoring_cpa_responses_model", "") or "").strip()
        or responses_model
    )

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
            "monitoring_cpa_base_url": bool(cpa_base_url),
            "monitoring_cpa_api_key": bool(
                (getattr(settings, "monitoring_cpa_api_key", "") or "").strip()
            ),
        },
        "public_base_url": public_base_url or None,
        "gateway_health_url": gateway_health_url or None,
        "cpa_base_url": cpa_base_url or None,
        "probe_models": {
            "chat_completions": chat_model or None,
            "responses": responses_model or None,
            "cpa_chat_completions": cpa_chat_model or None,
            "cpa_responses": cpa_responses_model or None,
        },
        "recommended_architecture": {
            "frontend": "管理员后台展示双层监控说明、配置状态、探针结果，不做终端用户状态页。",
            "backend": "由受保护的 ops probes 承担真实探测。clawfather probes 监控公网控制面，CPA probes 直连旧 GPT/Codex lane，用于分层定位故障。",
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
                {"name": "cpa-public-health", "method": "GET", "path": "/ops/monitoring/probes/cpa-public-health", "optional": True},
                {"name": "cpa-catalog", "method": "GET", "path": "/ops/monitoring/probes/cpa-catalog", "optional": True},
                {"name": "cpa-chat-completions", "method": "POST", "path": "/ops/monitoring/probes/cpa-chat-completions", "optional": True},
                {"name": "cpa-responses", "method": "POST", "path": "/ops/monitoring/probes/cpa-responses", "optional": True},
            ],
        },
        "monitoring_layers": [
            {
                "name": "clawfather",
                "scope": "公网控制面与经 coincoin-proxy 分发后的真实用户路径",
                "base_url": public_base_url or None,
            },
            {
                "name": "cpa_direct",
                "scope": "绕过 clawfather，直连 CPA / legacy GPT-Codex lane",
                "base_url": cpa_base_url or None,
            },
        ],
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


def _recommended_probe_definitions() -> list[Dict[str, Any]]:
    return list(build_monitoring_summary()["checkly"]["recommended_checks"])


def _probe_group_for(probe_name: str) -> str:
    if probe_name.startswith("cpa-"):
        return "cpa_direct"
    if probe_name == "gateway-readiness":
        return "gateway"
    return "clawfather"


def _probe_capability_for(probe_name: str) -> str:
    if probe_name in {"chat-completions", "cpa-chat-completions"}:
        return "chat"
    if probe_name in {"chat-stream"}:
        return "stream"
    if probe_name in {"responses", "cpa-responses"}:
        return "responses"
    if probe_name in {"catalog", "cpa-catalog"}:
        return "catalog"
    return "health"


async def build_monitoring_snapshot() -> Dict[str, Any]:
    summary = build_monitoring_summary()
    checks = _recommended_probe_definitions()

    async def _run_check(check: Dict[str, Any]) -> Dict[str, Any]:
        probe_name = str(check.get("name") or "")
        try:
            result = await _execute_probe(probe_name)
        except MonitoringConfigError as exc:
            result = _probe_result(
                probe=probe_name,
                ok=False,
                latency_ms=None,
                target="configuration",
                details={"error": str(exc)},
            )
        result["group"] = _probe_group_for(probe_name)
        result["capability"] = _probe_capability_for(probe_name)
        result["optional"] = bool(check.get("optional"))
        result["method"] = check.get("method") or "GET"
        result["path"] = check.get("path") or ""
        return result

    probe_results = await asyncio.gather(*[_run_check(check) for check in checks])
    probe_map = {item["probe"]: item for item in probe_results}

    group_order = ["clawfather", "cpa_direct", "gateway"]
    layer_specs = {
        "clawfather": {
            "title": "Clawfather",
            "subtitle": "公网入口与分发层",
            "base_url": summary.get("public_base_url"),
            "probe_names": [
                "public-health",
                "catalog",
                "chat-completions",
                "chat-stream",
                "responses",
            ],
        },
        "cpa_direct": {
            "title": "CPA Direct",
            "subtitle": "绕过 clawfather 的 legacy GPT/Codex lane",
            "base_url": summary.get("cpa_base_url"),
            "probe_names": [
                "cpa-public-health",
                "cpa-catalog",
                "cpa-chat-completions",
                "cpa-responses",
            ],
        },
        "gateway": {
            "title": "Gateway",
            "subtitle": "内部 Gemini / LiteLLM 数据面",
            "base_url": summary.get("gateway_health_url"),
            "probe_names": ["gateway-readiness"],
        },
    }

    layers = []
    total_required = 0
    total_ok = 0

    for group_name in group_order:
        spec = layer_specs[group_name]
        group_probes = [
            probe_map[name]
            for name in spec["probe_names"]
            if name in probe_map
        ]
        required_probes = [probe for probe in group_probes if not probe.get("optional")]
        ok_required = [probe for probe in required_probes if probe.get("ok")]
        total_required += len(required_probes)
        total_ok += len(ok_required)

        if required_probes:
            availability = round((len(ok_required) / len(required_probes)) * 100, 2)
        else:
            availability = None

        failing = [probe for probe in group_probes if not probe.get("ok")]
        layer_ok = all(probe.get("ok") for probe in required_probes) if required_probes else None
        latency_values = [
            int(probe["latency_ms"])
            for probe in group_probes
            if isinstance(probe.get("latency_ms"), int)
        ]
        max_latency = max(latency_values) if latency_values else None
        avg_latency = round(sum(latency_values) / len(latency_values)) if latency_values else None
        issue_summary = None
        if failing:
            first = failing[0]
            detail_error = str((first.get("details") or {}).get("error") or "").strip()
            http_status = (first.get("details") or {}).get("http_status")
            issue_summary = detail_error or (f"HTTP {http_status}" if http_status else "probe failed")

        status_label = "未启用"
        summary_label = "未接入"
        if layer_ok is True:
            status_label = "正常"
            summary_label = "运行稳定"
        elif layer_ok is False:
            status_label = "异常"
            summary_label = "存在异常"
        elif group_probes:
            status_label = "监控中"
            summary_label = "仅可选探针"

        primary_signal = "未接入"
        if group_name == "clawfather":
            if any(probe.get("probe") == "public-health" and probe.get("ok") for probe in group_probes):
                if any(
                    probe.get("probe") in {"chat-completions", "responses", "chat-stream"}
                    and not probe.get("ok")
                    for probe in group_probes
                ):
                    primary_signal = "入口活着，但真实对话链路异常"
                else:
                    primary_signal = "公网入口与真实对话链路正常"
            else:
                primary_signal = issue_summary or "公网入口不可用"
        elif group_name == "cpa_direct":
            if required_probes:
                primary_signal = "CPA 直连正常" if layer_ok else (issue_summary or "CPA 直连异常")
            else:
                primary_signal = issue_summary or "CPA 直连未启用"
        elif group_name == "gateway":
            if required_probes:
                primary_signal = "Gateway 正常" if layer_ok else (issue_summary or "Gateway 异常")
            else:
                primary_signal = issue_summary or "Gateway 健康检查未配置"

        layers.append(
            {
                "name": group_name,
                "title": spec["title"],
                "subtitle": spec["subtitle"],
                "base_url": spec["base_url"],
                "ok": layer_ok,
                "status_label": status_label,
                "summary_label": summary_label,
                "primary_signal": primary_signal,
                "availability_percent": availability,
                "required_probe_count": len(required_probes),
                "ok_probe_count": len(ok_required),
                "max_latency_ms": max_latency,
                "avg_latency_ms": avg_latency,
                "issue_summary": issue_summary,
                "probes": group_probes,
            }
        )

    overall_ok = total_ok == total_required if total_required else None
    overall_availability = round((total_ok / total_required) * 100, 2) if total_required else None
    all_latencies = [
        int(probe["latency_ms"])
        for probe in probe_results
        if isinstance(probe.get("latency_ms"), int) and not probe.get("optional")
    ]

    incident_message = "监控未启用"
    if overall_ok is True:
        incident_message = "当前主要链路运行稳定"
    elif overall_ok is False:
        claw_layer = next((layer for layer in layers if layer["name"] == "clawfather"), None)
        cpa_layer = next((layer for layer in layers if layer["name"] == "cpa_direct"), None)
        if claw_layer and claw_layer.get("ok") is False:
            incident_message = claw_layer.get("primary_signal") or "Clawfather 层存在异常"
        elif cpa_layer and cpa_layer.get("ok") is False:
            incident_message = cpa_layer.get("primary_signal") or "CPA 直连存在异常"
        else:
            incident_message = "存在需要处理的异常"

    action_items = []
    for layer in layers:
        if layer["name"] == "gateway" and not layer.get("base_url"):
            action_items.append("补充 Gateway Health URL，单独监控 Gemini 数据面")
        elif layer.get("ok") is False and layer.get("primary_signal"):
            action_items.append(layer["primary_signal"])
        elif layer["name"] == "cpa_direct" and layer.get("issue_summary") and "404" in str(layer.get("issue_summary")):
            action_items.append("检查 CPA Base URL，建议填写根域名而不是带 /v1 的路径")

    return {
        "checked_at": _utc_now(),
        "overall": {
            "ok": overall_ok,
            "status_label": "正常" if overall_ok is True else "异常" if overall_ok is False else "未启用",
            "incident_message": incident_message,
            "availability_percent": overall_availability,
            "required_probe_count": total_required,
            "ok_probe_count": total_ok,
            "max_latency_ms": max(all_latencies) if all_latencies else None,
            "avg_latency_ms": round(sum(all_latencies) / len(all_latencies)) if all_latencies else None,
        },
        "action_items": action_items[:4],
        "summary": summary,
        "layers": layers,
        "probes": probe_results,
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


async def run_cpa_public_health_probe() -> Dict[str, Any]:
    cfg = _require_cpa_probe_config()
    url = _join_url(cfg["base_url"], "/healthz")
    result = await _request_json("GET", url)
    body = result["body"]
    ok = result["status_code"] == 200 and isinstance(body, dict) and body.get("status") == "ok"
    return _probe_result(
        probe="cpa-public-health",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "body": body if isinstance(body, dict) else None,
        },
    )


async def run_cpa_catalog_probe() -> Dict[str, Any]:
    cfg = _require_cpa_probe_config()
    url = _join_url(cfg["base_url"], "/v1/models")
    headers = _monitoring_headers(cfg["api_key"])
    result = await _request_json("GET", url, headers=headers)
    body = result["body"]
    data = body.get("data") if isinstance(body, dict) else None
    chat_model = (
        str(getattr(settings, "monitoring_cpa_chat_model", "") or "").strip()
        or _resolve_probe_model("monitoring_chat_model", "chat/completions")
    )
    responses_model = (
        str(getattr(settings, "monitoring_cpa_responses_model", "") or "").strip()
        or _resolve_probe_model("monitoring_responses_model", "responses")
    )
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
        probe="cpa-catalog",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "model_count": len(returned_ids),
            "monitoring_cpa_chat_model": chat_model or None,
            "monitoring_cpa_responses_model": responses_model or None,
            "returned_model_ids": returned_ids[:25],
        },
    )


async def run_cpa_chat_completions_probe() -> Dict[str, Any]:
    cfg = _require_cpa_probe_config()
    model = (
        str(getattr(settings, "monitoring_cpa_chat_model", "") or "").strip()
        or _resolve_probe_model("monitoring_chat_model", "chat/completions")
    )
    if not model:
        raise MonitoringConfigError("missing monitoring_cpa_chat_model")

    url = _join_url(cfg["base_url"], "/v1/chat/completions")
    headers = _monitoring_headers(cfg["api_key"])
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 16,
        "messages": [
            {
                "role": "user",
                "content": f"Reply with exactly {_MARKER}. No punctuation.",
            }
        ],
    }
    result = await _request_json("POST", url, headers=headers, json_body=payload)
    body = result["body"]
    content = _extract_chat_content(body) if isinstance(body, dict) else ""
    normalized = content.strip().upper()
    ok = result["status_code"] == 200 and _MARKER in normalized
    return _probe_result(
        probe="cpa-chat-completions",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "model": model,
            "response_excerpt": content[:200],
        },
    )


async def run_cpa_responses_probe() -> Dict[str, Any]:
    cfg = _require_cpa_probe_config()
    model = (
        str(getattr(settings, "monitoring_cpa_responses_model", "") or "").strip()
        or _resolve_probe_model("monitoring_responses_model", "responses")
    )
    if not model:
        raise MonitoringConfigError("missing monitoring_cpa_responses_model")

    url = _join_url(cfg["base_url"], "/v1/responses")
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
        probe="cpa-responses",
        ok=ok,
        latency_ms=result["latency_ms"],
        target=url,
        details={
            "http_status": result["status_code"],
            "model": model,
            "response_excerpt": content[:200],
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
    if probe_name == "cpa-public-health":
        return await run_cpa_public_health_probe()
    if probe_name == "cpa-catalog":
        return await run_cpa_catalog_probe()
    if probe_name == "cpa-chat-completions":
        return await run_cpa_chat_completions_probe()
    if probe_name == "cpa-responses":
        return await run_cpa_responses_probe()
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


@ops_router.get("/snapshot", dependencies=[Depends(require_monitoring)])
async def ops_monitoring_snapshot() -> Dict[str, Any]:
    return await build_monitoring_snapshot()


@ops_router.get("/probes/{probe_name}", dependencies=[Depends(require_monitoring)])
async def ops_run_probe(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)


@ops_router.post("/probes/{probe_name}", dependencies=[Depends(require_monitoring)])
async def ops_run_probe_post(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)


@admin_router.get("/summary", dependencies=[Depends(_admin_guard)])
async def admin_monitoring_summary() -> Dict[str, Any]:
    return build_monitoring_summary()


@admin_router.get("/snapshot", dependencies=[Depends(_admin_guard)])
async def admin_monitoring_snapshot() -> Dict[str, Any]:
    return await build_monitoring_snapshot()


@admin_router.get("/probes/{probe_name}", dependencies=[Depends(_admin_guard)])
async def admin_run_probe(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)


@admin_router.post("/probes/{probe_name}", dependencies=[Depends(_admin_guard)])
async def admin_run_probe_post(probe_name: str) -> JSONResponse:
    return await _run_probe_response(probe_name)
