from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .proxy import (
    _build_upstream_headers,
    authenticate_user,
    authorize_request,
    extract_upstream_request_id,
    filter_headers,
    get_http_client,
    get_stream_client,
)
from .router import (
    ModelCapabilityError,
    UnknownModelError,
    build_model_cloak,
    registry as model_registry,
)
from .usage_buffer import usage_buffer


router = APIRouter(prefix="/v1", tags=["anthropic-compat"])


def anthropic_error(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        },
    )


def _model_resolution_to_anthropic_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, UnknownModelError):
        return anthropic_error(str(exc), status_code=400)
    if isinstance(exc, ModelCapabilityError):
        return anthropic_error(str(exc), status_code=400)
    return anthropic_error("Unable to resolve model", error_type="api_error", status_code=500)


def _coerce_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
    return "".join(text_parts)


def _anthropic_messages_to_openai_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []

    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        system_parts: List[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                system_parts.append(str(block.get("text") or ""))
        if system_parts:
            messages.append({"role": "system", "content": "".join(system_parts)})

    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return messages

    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        messages.append(
            {
                "role": role,
                "content": _coerce_message_text(item.get("content")),
            }
        )
    return messages


def _anthropic_tools_to_openai_tools(payload: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, list):
        return None

    tools: List[Dict[str, Any]] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return tools or None


def _build_anthropic_upstream_headers(cfg, request: Request) -> Dict[str, str]:
    headers = _build_upstream_headers(cfg)
    headers["content-type"] = "application/json"

    anthropic_version = request.headers.get("anthropic-version")
    if anthropic_version:
        headers["anthropic-version"] = anthropic_version
    else:
        headers["anthropic-version"] = "2023-06-01"

    anthropic_beta = request.headers.get("anthropic-beta")
    if anthropic_beta:
        headers["anthropic-beta"] = anthropic_beta

    user_agent = request.headers.get("user-agent")
    if user_agent:
        headers["user-agent"] = user_agent

    return headers


def _build_anthropic_response(
    *,
    display_model: str,
    public_model,
    content_text: str,
    usage: Dict[str, Any],
    response_id: str = "",
) -> Dict[str, Any]:
    return {
        "id": response_id or f"msg_coincoin_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": display_model,
        "content": [{"type": "text", "text": content_text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    }


def _extract_text_from_openai_chat_response(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        return "".join(text_parts)
    return ""


def _extract_usage_from_openai_chat_response(data: Dict[str, Any]) -> Dict[str, Any]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    return usage


@router.get("/models")
async def anthropic_models(request: Request):
    user_agent = request.headers.get("user-agent", "")
    if not user_agent.startswith("claude-cli"):
        from .openai_compat import list_models

        return await list_models(request)

    models = []
    for public_model in model_registry.list_public_models("chat/completions"):
        models.append(
            {
                "type": "model",
                "id": public_model.public_id,
                "display_name": public_model.public_id,
                "created_at": public_model.created,
            }
        )
    return {"data": models, "has_more": False, "first_id": models[0]["id"] if models else None, "last_id": models[-1]["id"] if models else None}


@router.post("/messages/count_tokens")
async def anthropic_count_tokens(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        await authenticate_user(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return anthropic_error("Invalid API key", error_type="authentication_error", status_code=401)
        if exc.status_code == 403:
            return anthropic_error("Access denied", error_type="permission_error", status_code=403)
        raise

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid json payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a json object")

    requested_model = str(payload.get("model") or "").strip()
    messages = _anthropic_messages_to_openai_messages(payload)
    tools = _anthropic_tools_to_openai_tools(payload)
    try:
        resolved_model = model_registry.resolve_public_model(requested_model, "chat/completions", messages, tools)
    except Exception as exc:
        return _model_resolution_to_anthropic_error(exc)

    text = ""
    for msg in messages:
        text += str(msg.get("content") or "")
    input_tokens = max(1, len(text.strip().split())) if text.strip() else 1
    return {"input_tokens": input_tokens}


@router.post("/messages")
async def anthropic_messages(request: Request, db: AsyncSession = Depends(get_db)):
    user = await authorize_request(request, db)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid json payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a json object")

    requested_model = str(payload.get("model") or "").strip()
    messages = _anthropic_messages_to_openai_messages(payload)
    tools = _anthropic_tools_to_openai_tools(payload)
    try:
        resolved_model = model_registry.resolve_public_model(requested_model, "chat/completions", messages, tools)
    except Exception as exc:
        return _model_resolution_to_anthropic_error(exc)

    public_model = resolved_model.public_model
    display_model = public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason

    openai_payload: Dict[str, Any] = {
        "model": used_cfg.model_id,
        "messages": messages,
        "stream": bool(payload.get("stream")),
    }

    if tools:
        openai_payload["tools"] = tools

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        openai_payload["max_tokens"] = max_tokens

    if settings.model_cloak and display_model and not tools:
        cloak = build_model_cloak(display_model, public_model)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = str(messages[0].get("content") or "") + cloak
        else:
            openai_payload["messages"] = [{"role": "system", "content": cloak.strip()}] + messages

    upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/messages"
    headers = _build_anthropic_upstream_headers(used_cfg, request)

    if openai_payload.get("stream"):
        stream_client = await get_stream_client()
        req = stream_client.build_request("POST", upstream_url, json=openai_payload, headers=headers)
        upstream = await stream_client.send(req, stream=True)
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        stream_headers = filter_headers(dict(upstream.headers))
        stream_headers.pop("content-length", None)
        stream_headers.setdefault("cache-control", "no-cache")
        stream_headers.setdefault("x-accel-buffering", "no")

        async def iter_bytes():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            iter_bytes(),
            status_code=upstream.status_code,
            headers=stream_headers,
            media_type=upstream.headers.get("content-type"),
        )

    client = await get_http_client()
    upstream = await client.post(upstream_url, json=openai_payload, headers=headers)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)
    upstream_request_id = extract_upstream_request_id(upstream.headers)

    content_type = upstream.headers.get("content-type", "application/json")
    if "application/json" not in content_type:
        body = await upstream.aread()
        return Response(content=body, status_code=upstream.status_code, headers=response_headers, media_type=content_type)

    try:
        data = upstream.json()
    except Exception:
        return anthropic_error("Upstream returned invalid JSON", error_type="api_error", status_code=502)

    if upstream.status_code >= 400:
        if isinstance(data, dict) and "error" in data:
            error_info = data["error"]
            message = error_info.get("message") if isinstance(error_info, dict) else str(error_info)
            error_type = error_info.get("type") if isinstance(error_info, dict) else "api_error"
            return anthropic_error(message or "Upstream request failed", error_type=error_type or "api_error", status_code=upstream.status_code)
        return anthropic_error("Upstream request failed", error_type="api_error", status_code=upstream.status_code)

    text = _extract_text_from_openai_chat_response(data)
    usage = _extract_usage_from_openai_chat_response(data)

    await usage_buffer.add(
        user.id,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=0,
        requests=1,
        endpoint="messages",
        model=display_model,
        customer_model_alias=display_model,
        provider_model=public_model.provider_model or used_cfg.model_id,
        route_reason=used_route_reason,
        duration_ms=0,
        status_code=upstream.status_code,
        price_input_per_million=used_cfg.price_input_per_million,
        price_output_per_million=used_cfg.price_output_per_million,
        usage_unit_type="tokens",
        billable_sku=public_model.billable_sku or display_model,
        upstream_request_id=upstream_request_id,
    )

    response_body = _build_anthropic_response(
        display_model=display_model,
        public_model=public_model,
        content_text=text,
        usage=usage,
        response_id=str(data.get("id") or ""),
    )
    return JSONResponse(content=response_body, status_code=upstream.status_code, headers=response_headers)
