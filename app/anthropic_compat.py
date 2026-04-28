from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
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


@dataclass
class _AnthropicToolStreamState:
    index: int
    block_index: int
    tool_id: str = ""
    name: str = ""
    started: bool = False
    stopped: bool = False


@dataclass
class _AnthropicStreamState:
    response_id: str = ""
    created_at: int = 0
    message_started: bool = False
    message_stopped: bool = False
    message_delta_sent: bool = False
    text_block_started: bool = False
    text_block_index: int = 0
    saw_tool_call: bool = False
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    next_block_index: int = 1
    tool_states: Dict[int, _AnthropicToolStreamState] = field(default_factory=dict)


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
        content = item.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            messages.append({"role": role, "content": ""})
            continue

        text_parts: List[str] = []
        assistant_tool_calls: List[Dict[str, Any]] = []

        def _flush_user_text() -> None:
            if role == "user" and text_parts:
                messages.append({"role": "user", "content": "".join(text_parts)})
                text_parts.clear()

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")

            if block_type == "text":
                text_parts.append(str(block.get("text") or ""))
                continue

            if role == "assistant" and block_type == "tool_use":
                tool_id = str(block.get("id") or f"call_{len(assistant_tool_calls)}")
                tool_name = str(block.get("name") or "")
                tool_input = block.get("input")
                if isinstance(tool_input, str):
                    arguments = tool_input
                else:
                    arguments = json.dumps(tool_input or {}, ensure_ascii=False)
                assistant_tool_calls.append(
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": arguments},
                    }
                )
                continue

            if role == "user" and block_type == "tool_result":
                _flush_user_text()
                result_content = block.get("content")
                result_parts: List[str] = []
                if isinstance(result_content, str):
                    result_parts.append(result_content)
                elif isinstance(result_content, list):
                    for result_block in result_content:
                        if isinstance(result_block, dict) and result_block.get("type") == "text":
                            result_parts.append(str(result_block.get("text") or ""))

                tool_message: Dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": "".join(result_parts),
                }
                if tool_message["tool_call_id"]:
                    messages.append(tool_message)
                continue

        if role == "user":
            _flush_user_text()
            continue

        message: Dict[str, Any] = {"role": role, "content": "".join(text_parts)}
        if assistant_tool_calls:
            message["tool_calls"] = assistant_tool_calls
            if not text_parts:
                message["content"] = None
        messages.append(message)
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
    message_content: List[Dict[str, Any]],
    stop_reason: str,
    usage: Dict[str, Any],
    response_id: str = "",
) -> Dict[str, Any]:
    return {
        "id": response_id or f"msg_coincoin_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": display_model,
        "content": message_content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    }


def _extract_anthropic_content_from_openai_chat_response(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return []
    content_blocks: List[Dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str):
        if content:
            content_blocks.append({"type": "text", "text": content})
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                content_blocks.append({"type": "text", "text": str(block.get("text") or "")})

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    parsed_input = json.loads(arguments)
                except Exception:
                    parsed_input = {"raw": arguments}
            elif isinstance(arguments, dict):
                parsed_input = arguments
            else:
                parsed_input = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": str(tool_call.get("id") or f"call_{index}"),
                    "name": str(function.get("name") or ""),
                    "input": parsed_input,
                }
            )
    return content_blocks


def _extract_anthropic_stop_reason_from_openai_chat_response(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "end_turn"
    finish_reason = str(choices[0].get("finish_reason") or choices[0].get("native_finish_reason") or "")
    return _normalize_anthropic_stop_reason(finish_reason, finish_reason == "tool_calls")


def _extract_usage_from_openai_chat_response(data: Dict[str, Any]) -> Dict[str, Any]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    return usage


def _anthropic_sse_bytes(event_type: str, payload: Dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


def _normalize_anthropic_stop_reason(finish_reason: str, saw_tool_call: bool) -> str:
    reason = str(finish_reason or "").strip()
    if saw_tool_call or reason == "tool_calls":
        return "tool_use"
    if reason in {"length", "max_tokens"}:
        return "max_tokens"
    return "end_turn"


def _ensure_anthropic_message_start(
    state: _AnthropicStreamState,
    *,
    display_model: str,
    response_id: str,
) -> List[bytes]:
    if state.message_started:
        return []
    state.message_started = True
    state.response_id = response_id or state.response_id or f"msg_coincoin_{int(time.time() * 1000)}"
    payload = {
        "type": "message_start",
        "message": {
            "id": state.response_id,
            "type": "message",
            "role": "assistant",
            "model": display_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    return [_anthropic_sse_bytes("message_start", payload)]


def _start_text_block(state: _AnthropicStreamState) -> List[bytes]:
    if state.text_block_started:
        return []
    state.text_block_started = True
    state.next_block_index = max(state.next_block_index, state.text_block_index + 1)
    payload = {
        "type": "content_block_start",
        "index": state.text_block_index,
        "content_block": {"type": "text", "text": ""},
    }
    return [_anthropic_sse_bytes("content_block_start", payload)]


def _stop_text_block(state: _AnthropicStreamState) -> List[bytes]:
    if not state.text_block_started:
        return []
    state.text_block_started = False
    payload = {"type": "content_block_stop", "index": state.text_block_index}
    return [_anthropic_sse_bytes("content_block_stop", payload)]


def _ensure_tool_state(state: _AnthropicStreamState, tool_index: int) -> _AnthropicToolStreamState:
    tool_state = state.tool_states.get(tool_index)
    if tool_state is None:
        tool_state = _AnthropicToolStreamState(index=tool_index, block_index=state.next_block_index)
        state.tool_states[tool_index] = tool_state
        state.next_block_index += 1
    return tool_state


def _start_tool_block(tool_state: _AnthropicToolStreamState) -> List[bytes]:
    if tool_state.started:
        return []
    tool_state.started = True
    payload = {
        "type": "content_block_start",
        "index": tool_state.block_index,
        "content_block": {
            "type": "tool_use",
            "id": tool_state.tool_id or f"call_{tool_state.index}",
            "name": tool_state.name or f"tool_{tool_state.index}",
            "input": {},
        },
    }
    return [_anthropic_sse_bytes("content_block_start", payload)]


def _stop_tool_blocks(state: _AnthropicStreamState) -> List[bytes]:
    events: List[bytes] = []
    for tool_index in sorted(state.tool_states):
        tool_state = state.tool_states[tool_index]
        if not tool_state.started or tool_state.stopped:
            continue
        tool_state.stopped = True
        payload = {"type": "content_block_stop", "index": tool_state.block_index}
        events.append(_anthropic_sse_bytes("content_block_stop", payload))
    return events


def _finalize_anthropic_stream(
    state: _AnthropicStreamState,
    *,
    display_model: str,
) -> List[bytes]:
    if state.message_stopped:
        return []

    events: List[bytes] = []
    if not state.message_started:
        events.extend(_ensure_anthropic_message_start(state, display_model=display_model, response_id=state.response_id))

    events.extend(_stop_text_block(state))
    events.extend(_stop_tool_blocks(state))

    if not state.message_delta_sent:
        state.message_delta_sent = True
        usage = {
            "input_tokens": int(state.usage.get("prompt_tokens") or state.usage.get("input_tokens") or 0),
            "output_tokens": int(state.usage.get("completion_tokens") or state.usage.get("output_tokens") or 0),
        }
        payload = {
            "type": "message_delta",
            "delta": {
                "stop_reason": _normalize_anthropic_stop_reason(state.finish_reason, state.saw_tool_call),
                "stop_sequence": None,
            },
            "usage": usage,
        }
        events.append(_anthropic_sse_bytes("message_delta", payload))

    state.message_stopped = True
    events.append(_anthropic_sse_bytes("message_stop", {"type": "message_stop"}))
    return events


def _translate_openai_chunk_to_anthropic_events(
    state: _AnthropicStreamState,
    *,
    display_model: str,
    raw_line: str,
) -> List[bytes]:
    line = raw_line.strip()
    if not line or not line.startswith("data:"):
        return []

    payload = line[5:].strip()
    if not payload:
        return []
    if payload == "[DONE]":
        return _finalize_anthropic_stream(state, display_model=display_model)

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, dict):
        return []

    events: List[bytes] = []
    response_id = str(data.get("id") or state.response_id or "")
    if response_id:
        state.response_id = response_id
    created = data.get("created")
    if isinstance(created, int) and created > 0:
        state.created_at = created

    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}

    if delta:
        events.extend(_ensure_anthropic_message_start(state, display_model=display_model, response_id=response_id))

    content = delta.get("content")
    if isinstance(content, str) and content:
        events.extend(_start_text_block(state))
        payload = {
            "type": "content_block_delta",
            "index": state.text_block_index,
            "delta": {"type": "text_delta", "text": content},
        }
        events.append(_anthropic_sse_bytes("content_block_delta", payload))

    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        state.saw_tool_call = True
        events.extend(_stop_text_block(state))
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            tool_index = int(item.get("index") or 0)
            tool_state = _ensure_tool_state(state, tool_index)
            if item.get("id"):
                tool_state.tool_id = str(item.get("id") or tool_state.tool_id)
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            if function.get("name"):
                tool_state.name = str(function.get("name") or tool_state.name)
            events.extend(_start_tool_block(tool_state))
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                payload = {
                    "type": "content_block_delta",
                    "index": tool_state.block_index,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                }
                events.append(_anthropic_sse_bytes("content_block_delta", payload))

    finish_reason = choice.get("finish_reason") or choice.get("native_finish_reason")
    if isinstance(finish_reason, str) and finish_reason:
        state.finish_reason = finish_reason

    usage = data.get("usage")
    if isinstance(usage, dict):
        state.usage = usage

    return events


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

    upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/chat/completions"
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
        stream_headers["content-type"] = "text/event-stream; charset=utf-8"
        stream_state = _AnthropicStreamState()

        async def iter_bytes():
            try:
                async for line in upstream.aiter_lines():
                    for event in _translate_openai_chunk_to_anthropic_events(
                        stream_state,
                        display_model=display_model,
                        raw_line=line,
                    ):
                        yield event
            finally:
                if not stream_state.message_stopped:
                    for event in _finalize_anthropic_stream(stream_state, display_model=display_model):
                        yield event
                if stream_state.usage:
                    await usage_buffer.add(
                        user.id,
                        input_tokens=int(stream_state.usage.get("prompt_tokens") or stream_state.usage.get("input_tokens") or 0),
                        output_tokens=int(stream_state.usage.get("completion_tokens") or stream_state.usage.get("output_tokens") or 0),
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
                await upstream.aclose()

        return StreamingResponse(
            iter_bytes(),
            status_code=upstream.status_code,
            headers=stream_headers,
            media_type="text/event-stream",
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

    content_blocks = _extract_anthropic_content_from_openai_chat_response(data)
    usage = _extract_usage_from_openai_chat_response(data)
    stop_reason = _extract_anthropic_stop_reason_from_openai_chat_response(data)

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
        message_content=content_blocks,
        stop_reason=stop_reason,
        usage=usage,
        response_id=str(data.get("id") or ""),
    )
    return JSONResponse(content=response_body, status_code=upstream.status_code, headers=response_headers)
