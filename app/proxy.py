import asyncio
import base64
import ipaddress
import json
import logging
import secrets
import time
from urllib.parse import urlsplit, urlunsplit
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import settings
from .channel_router import channel_router, should_record_failure as should_record_channel_failure
from .db import get_db
from .fallback_alerts import FallbackExhaustedAlert, notify_fallback_exhausted
from . import gemini_cpa
from .models import ApiKey, RequestLog, UsageDaily, User
from .prompt_cache import build_claude_code_prompt_cache_key
from .rate_limiter import rate_limiter
from .security import extract_api_key, hash_key
from .station_runtime import (
    resolve_station_model_for_user,
    station_context_for_user,
    usage_pricing_kwargs,
)
from .router import (
    CLAUDE_COMPAT_PROVIDER_KIRO_GO,
    ModelCapabilityError,
    UnknownModelError,
    extract_messages_for_routing_from_responses_payload,
    registry as model_registry,
)
from .usage_buffer import (
    china_today,
    extract_cache_creation_tokens,
    extract_cache_read_tokens,
    extract_total_input_tokens,
    usage_buffer,
)

_KEY_KIND_ATTR = "_key_kind"
_KEY_ID_ATTR = "_api_key_id"
CHANNEL_FALLBACK_MAX_ATTEMPTS = 2
CHANNEL_FALLBACK_RETRY_ERRORS = frozenset({
    "upstream_unreachable",
    "upstream_timeout",
    "upstream_invalid_json",
    "upstream_unexpected_content_type",
    "upstream_empty_response",
})
_ENCRYPTED_PREFIXES = ("gAAA", "gBAA")
_ID_STRIP_PREFIXES = ("resp_", "msg_", "fc_", "fco_", "rs_")
_CONTENT_KEYS = frozenset({
    "text", "content", "output", "arguments", "instructions",
    "description", "name", "url", "title",
})
_PROTOCOL_ENCRYPTED_KEYS = frozenset({"encrypted_content"})


def _is_encrypted_blob(v: str) -> bool:
    """Check if a string IS an encrypted blob, not just contains one."""
    if v.startswith(_ENCRYPTED_PREFIXES):
        return True
    for p in _ID_STRIP_PREFIXES:
        if v.startswith(p) and v[len(p):].startswith(_ENCRYPTED_PREFIXES):
            return True
    return False


def _sanitize_encrypted_ids(payload: dict) -> None:
    """Recursively replace/remove ChatGPT-encrypted ID blobs from the request.

    Only touches fields whose ENTIRE value is an encrypted blob (with optional
    API prefix like fc_, msg_, resp_).  Content fields (text, output,
    arguments, etc.) are never modified — they may legitimately contain the
    substring 'gAAA' in user text, code, or error messages.
    """
    _PREFIX_BY_TYPE = {
        "function_call": "fc",
        "function_call_output": "fco",
        "message": "msg",
    }
    id_map: dict[str, str] = {}
    counter = [0]

    def _next_id(prefix: str, original: str) -> str:
        if original not in id_map:
            counter[0] += 1
            id_map[original] = f"{prefix}_{counter[0]:04d}"
        return id_map[original]

    def _walk(obj, item_type: str = ""):
        if isinstance(obj, dict):
            cur_type = obj.get("type", "") or item_type
            to_delete: list[str] = []
            for k, v in obj.items():
                if isinstance(v, str) and _is_encrypted_blob(v):
                    if k in _PROTOCOL_ENCRYPTED_KEYS:
                        continue
                    if k in _CONTENT_KEYS:
                        obj[k] = ""
                    elif k == "call_id":
                        obj[k] = _next_id("call", v)
                    elif k == "id":
                        prefix = _PREFIX_BY_TYPE.get(cur_type, "id")
                        obj[k] = _next_id(prefix, v)
                    else:
                        to_delete.append(k)
                elif isinstance(v, (dict, list)):
                    _walk(v, cur_type)
            for k in to_delete:
                del obj[k]
        elif isinstance(obj, list):
            i = 0
            while i < len(obj):
                item = obj[i]
                if isinstance(item, str) and _is_encrypted_blob(item):
                    del obj[i]
                elif isinstance(item, (dict, list)):
                    _walk(item)
                    i += 1
                else:
                    i += 1

    _walk(payload)


def _ensure_content_text(payload: dict) -> None:
    """Ensure every content item has 'text'; drop unsupported types (reasoning/thinking)."""
    inp = payload.get("input")
    if not isinstance(inp, list):
        return
    for msg in inp:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        cleaned = []
        for c in content:
            if not isinstance(c, dict):
                cleaned.append(c)
                continue
            ct = c.get("type", "")
            if ct in ("reasoning", "thinking"):
                continue
            role = msg.get("role", "user")
            if ct == "text" or ct == "":
                c["type"] = "output_text" if role == "assistant" else "input_text"
            if c.get("type") in ("input_text", "output_text") and ("text" not in c or c.get("text") is None):
                c["text"] = ""
            cleaned.append(c)
        msg["content"] = cleaned if cleaned else [{"type": "input_text", "text": ""}]


def _normalize_openai_image_base_url(base_url: str) -> str:
    cleaned = str(base_url or "").strip()
    while cleaned.endswith("}"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.rstrip("/")
    if not cleaned:
        return cleaned

    parsed = urlsplit(cleaned)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"

    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _normalize_openai_base_url(base_url: str) -> str:
    cleaned = str(base_url or "").strip()
    while cleaned.endswith("}"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.rstrip("/")
    if not cleaned:
        return cleaned

    parsed = urlsplit(cleaned)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _build_openai_image_upstream_url(base_url: str, endpoint: str) -> str:
    normalized = _normalize_openai_image_base_url(base_url)
    return f"{normalized}/{endpoint.lstrip('/')}"


def _build_openai_responses_upstream_url(base_url: str) -> str:
    return f"{_normalize_openai_base_url(base_url)}/responses"


def _responses_text_input_item(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text or ""}],
    }


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else ""


def _parse_ip_allowlist(raw: object) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except Exception:
        pass
    return [item.strip() for item in text.replace("\n", ",").split(",") if item.strip()]


def _ip_allowed(client_ip: str, allowlist: List[str]) -> bool:
    if not allowlist:
        return True
    try:
        parsed_ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for raw in allowlist:
        try:
            if parsed_ip in ipaddress.ip_network(raw, strict=False):
                return True
        except ValueError:
            continue
    return False


def _month_start_utc() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


def _clone_responses_items(raw_items) -> list:
    if not isinstance(raw_items, list):
        return []
    return [deepcopy(item) for item in raw_items if isinstance(item, dict)]


def _normalize_responses_input_items(raw_input) -> list:
    """Normalize Responses API input into a list of input items for cache/polyfill use."""
    if isinstance(raw_input, str):
        return [_responses_text_input_item(raw_input)]
    if not isinstance(raw_input, list):
        return []

    normalized = []
    text_parts = []

    def _flush_text_parts() -> None:
        if text_parts:
            normalized.append(_responses_text_input_item("".join(text_parts)))
            text_parts.clear()

    for item in raw_input:
        if isinstance(item, dict):
            _flush_text_parts()
            normalized.append(deepcopy(item))
        elif isinstance(item, str):
            # Heal legacy bad cache entries where a string input was split into chars.
            text_parts.append(item)
        else:
            _flush_text_parts()

    _flush_text_parts()
    return normalized


def _responses_payload_has_meaningful_output(resp: dict) -> bool:
    if not isinstance(resp, dict):
        return False

    if isinstance(resp.get("output_text"), str) and resp.get("output_text").strip():
        return True

    output = resp.get("output")
    if not isinstance(output, list):
        return False

    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("function_call", "tool_use", "function"):
            return True
        if item_type in ("output_text", "text") and str(item.get("text") or "").strip():
            return True
        if item_type == "message":
            for content_item in item.get("content") or []:
                if not isinstance(content_item, dict):
                    continue
                content_type = content_item.get("type")
                if content_type in ("function_call", "tool_use", "function"):
                    return True
                if content_type in ("output_text", "text") and str(content_item.get("text") or "").strip():
                    return True
    return False


def _responses_payload_is_empty_success(resp: dict) -> bool:
    if not isinstance(resp, dict) or resp.get("error"):
        return False
    if _responses_payload_has_meaningful_output(resp):
        return False

    status = str(resp.get("status") or "").strip().lower()
    usage = resp.get("usage") or {}
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    output = resp.get("output")
    has_output_container = isinstance(output, list)

    return has_output_container or status == "completed" or output_tokens > 0


def _responses_input_to_chat_messages(raw_input) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input or " "})
        return messages
    if not isinstance(raw_input, list):
        return messages

    for item in raw_input:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item or " "})
            continue
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type") or "")
        if item_type == "message" or "role" in item:
            role = str(item.get("role") or "user").strip().lower()
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            content = item.get("content")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                messages.append({"role": role, "content": str(item.get("text") or "")})
                continue

            text_parts: List[str] = []
            assistant_tool_calls: List[Dict[str, Any]] = []
            tool_results: List[Dict[str, Any]] = []
            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                    continue
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "")
                if part_type in {"input_text", "output_text", "text"}:
                    text_parts.append(str(part.get("text") or ""))
                    continue
                if part_type == "tool_result":
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(part.get("tool_use_id") or ""),
                            "content": str(part.get("content") or ""),
                        }
                    )
                    continue
                if part_type == "tool_use" and role == "assistant":
                    tool_input = part.get("input")
                    arguments = tool_input if isinstance(tool_input, str) else json.dumps(tool_input or {}, ensure_ascii=False)
                    assistant_tool_calls.append(
                        {
                            "id": str(part.get("id") or f"call_{secrets.token_hex(8)}"),
                            "type": "function",
                            "function": {
                                "name": str(part.get("name") or ""),
                                "arguments": arguments,
                            },
                        }
                    )
            message: Dict[str, Any] = {"role": role, "content": "".join(text_parts)}
            if assistant_tool_calls:
                message["tool_calls"] = assistant_tool_calls
                if not text_parts:
                    message["content"] = None
            if role != "tool":
                messages.append(message)
            for tool_item in tool_results:
                if tool_item.get("tool_call_id"):
                    messages.append(tool_item)
            continue

        if item_type in {"input_text", "text"}:
            messages.append({"role": "user", "content": str(item.get("text") or " ")})
            continue
        if item_type == "output_text":
            messages.append({"role": "assistant", "content": str(item.get("text") or "")})
            continue
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or item.get("tool_call_id") or ""),
                    "content": str(item.get("output") or ""),
                }
            )
    return messages


def _responses_tools_to_chat_tools(raw_tools) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(raw_tools, list):
        return None
    tools: List[Dict[str, Any]] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            tools.append(tool)
            continue
        if tool.get("type") == "function" and tool.get("name"):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                }
            )
            continue
        if tool.get("name"):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                }
            )
    return tools or None


def _safe_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _responses_usage_from_chat_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    prompt_tokens = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion_tokens = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total_tokens = _safe_int(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)
    response_usage: Dict[str, Any] = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cached_tokens = _safe_int(prompt_details.get("cached_tokens"))
    if cached_tokens:
        response_usage["input_tokens_details"] = {"cached_tokens": cached_tokens}
    return response_usage


def _translate_chat_response_to_responses(data: Dict[str, Any], display_model: str) -> Dict[str, Any]:
    output: List[Dict[str, Any]] = []
    text_parts: List[str] = []

    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        text = ""
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            content_text_parts: List[str] = []
            for part in content:
                if isinstance(part, str):
                    content_text_parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    content_text_parts.append(part["text"])
            text = "".join(content_text_parts)
        if text:
            text_parts.append(text)
            output.append(
                {
                    "id": f"msg_{secrets.token_hex(20)}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                            "logprobs": [],
                        }
                    ],
                }
            )
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            call_id = str(tool_call.get("id") or f"call_{secrets.token_hex(20)}")
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            output.append(
                {
                    "id": call_id,
                    "call_id": call_id,
                    "type": "function_call",
                    "name": str(function.get("name") or tool_call.get("name") or ""),
                    "arguments": str(arguments or ""),
                }
            )

    return {
        "id": f"resp_{secrets.token_hex(20)}",
        "object": "response",
        "created_at": int(data.get("created") or time.time()),
        "status": "completed",
        "model": display_model,
        "output": output,
        "output_text": "".join(text_parts),
        "usage": _responses_usage_from_chat_usage(data.get("usage") or {}),
    }


def _chat_completion_chunk_line(
    *,
    stream_id: str,
    display_model: str,
    delta: Dict[str, Any],
    finish_reason: Any = None,
) -> str:
    chunk = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": display_model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _responses_sse_line(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_responses_output_text_item(text: str) -> dict:
    return {
        "id": f"msg_{secrets.token_hex(20)}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
                "logprobs": [],
            }
        ],
    }


def _build_responses_function_call_item(item: Optional[dict] = None) -> dict:
    item = item or {}
    func = item.get("function") if isinstance(item.get("function"), dict) else {}
    call_id = str(item.get("call_id") or item.get("id") or f"call_{secrets.token_hex(20)}")
    arguments = item.get("arguments")
    if arguments is None:
        arguments = func.get("arguments", "")
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, ensure_ascii=False)
    elif arguments is None:
        arguments = ""
    return {
        "id": str(item.get("id") or call_id),
        "call_id": call_id,
        "type": "function_call",
        "name": str(item.get("name") or func.get("name") or ""),
        "arguments": str(arguments),
    }


async def _collect_responses_event_stream_payload(upstream: httpx.Response) -> dict:
    """Collapse a Responses SSE stream into a single JSON payload.

    Legacy GPT upstreams have started returning empty `output=[]` for non-stream
    JSON responses while still emitting correct `response.output_text.delta`
    and function-call events on the streaming path. For non-stream callers we
    can safely aggregate those events back into a normal Responses payload.
    """
    latest_response: Dict[str, object] = {}
    latest_usage: Dict[str, object] = {}
    text_parts: List[str] = []
    tool_calls: List[dict] = []
    tool_call_indexes: Dict[str, int] = {}
    current_tool_index: Optional[int] = None
    error_payload: Optional[dict] = None

    def _upsert_tool_call(item: Optional[dict]) -> Optional[int]:
        nonlocal current_tool_index
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "")
        if item_type not in {"function_call", "tool_use", "function"}:
            return None

        normalized = _build_responses_function_call_item(item)
        key = str(normalized.get("id") or normalized.get("call_id") or "")
        idx = tool_call_indexes.get(key) if key else None
        if idx is None:
            idx = len(tool_calls)
            tool_calls.append(normalized)
            if key:
                tool_call_indexes[key] = idx
        else:
            existing = tool_calls[idx]
            if normalized.get("name"):
                existing["name"] = normalized["name"]
            normalized_args = normalized.get("arguments")
            if isinstance(normalized_args, str) and normalized_args:
                existing_args = existing.get("arguments") or ""
                if not existing_args or len(normalized_args) >= len(existing_args):
                    existing["arguments"] = normalized_args
            if normalized.get("call_id"):
                existing["call_id"] = normalized["call_id"]
        current_tool_index = idx
        return idx

    async for line in upstream.aiter_lines():
        if not line:
            continue
        if line.startswith("event:"):
            continue
        if not line.startswith("data:"):
            continue

        payload_str = line[5:].strip()
        if payload_str == "[DONE]":
            break

        try:
            event = json.loads(payload_str)
        except Exception:
            continue

        if not isinstance(event, dict):
            continue

        if isinstance(event.get("error"), dict):
            error_payload = event["error"]
            continue

        response_obj = event.get("response")
        if isinstance(response_obj, dict):
            latest_response = response_obj
            usage = response_obj.get("usage")
            if isinstance(usage, dict):
                latest_usage = usage

        usage = event.get("usage")
        if isinstance(usage, dict):
            latest_usage = usage

        event_type = str(event.get("type") or "")
        if event_type in {"response.output_text.delta", "response.output_text.chunk"}:
            delta = event.get("delta")
            if isinstance(delta, dict):
                delta = delta.get("text")
            if isinstance(delta, str) and delta:
                text_parts.append(delta)
        elif event_type in {"response.output_item.added", "response.function_call_arguments.start", "response.output_item.done"}:
            _upsert_tool_call(event.get("item"))
        elif event_type == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if isinstance(delta, dict):
                delta = delta.get("arguments") or delta.get("text")
            if isinstance(delta, str) and delta:
                if current_tool_index is None and tool_calls:
                    current_tool_index = len(tool_calls) - 1
                if current_tool_index is not None:
                    tool_calls[current_tool_index]["arguments"] = str(tool_calls[current_tool_index].get("arguments") or "") + delta
        elif event_type == "response.function_call_arguments.done":
            arguments = event.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            if isinstance(arguments, str) and arguments and current_tool_index is not None:
                tool_calls[current_tool_index]["arguments"] = arguments
            current_tool_index = None

    if error_payload:
        return {"error": error_payload}

    data = dict(latest_response)
    if latest_usage and not isinstance(data.get("usage"), dict):
        data["usage"] = latest_usage

    collapsed_text = "".join(text_parts).strip()
    aggregated_output: List[dict] = []
    if collapsed_text:
        aggregated_output.append(_build_responses_output_text_item(collapsed_text))
    if tool_calls:
        aggregated_output.extend(tool_calls)

    if aggregated_output and _responses_payload_is_empty_success(data):
        data["output"] = aggregated_output
        data["output_text"] = collapsed_text
        data["status"] = "completed"

    if collapsed_text and not data.get("output_text"):
        data["output_text"] = collapsed_text

    if aggregated_output and not _responses_payload_has_meaningful_output(data):
        data["output"] = aggregated_output
        data["status"] = "completed"

    if not data:
        data = {
            "id": f"resp_{secrets.token_hex(20)}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "output": aggregated_output,
            "output_text": collapsed_text,
            "usage": latest_usage,
        }

    return data


def _expand_previous_response_input(payload: dict, cached_conv: Optional[Tuple[list, list]]) -> Optional[Tuple[int, int, int]]:
    if not cached_conv:
        return None

    prev_input, prev_output = cached_conv
    prev_input_items = _normalize_responses_input_items(prev_input)
    prev_output_items = _clone_responses_items(prev_output)
    cur_input_items = _normalize_responses_input_items(payload.get("input"))

    payload["input"] = prev_input_items + prev_output_items + cur_input_items
    return len(prev_input_items), len(prev_output_items), len(cur_input_items)


def _apply_previous_response_polyfill(
    payload: dict,
    cached_conv: Optional[Tuple[list, list]],
) -> Optional[Tuple[int, int, int]]:
    """Expand cached history locally and stop forwarding previous_response_id upstream.

    Once we replay the prior turns into `input`, keeping `previous_response_id`
    would ask the upstream to apply the same history again. That can inflate the
    effective context window turn after turn and cause long-tail latency.
    """
    counts = _expand_previous_response_input(payload, cached_conv)
    if counts is None:
        return None
    payload.pop("previous_response_id", None)
    return counts


router = APIRouter(prefix="/openai/v1", tags=["proxy"])
logger = logging.getLogger("coincoin.proxy")

_http_client: Optional[httpx.AsyncClient] = None
_http_stream_client: Optional[httpx.AsyncClient] = None
_http_image_stream_client: Optional[httpx.AsyncClient] = None
_http_lock = asyncio.Lock()
IMAGE_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=60.0)


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client and not _http_client.is_closed:
        return _http_client
    async with _http_lock:
        if _http_client and not _http_client.is_closed:
            return _http_client
        limits = httpx.Limits(
            max_connections=settings.http_pool_max,
            max_keepalive_connections=settings.http_pool_keepalive,
        )
        _http_client = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0),
            trust_env=False,
        )
        return _http_client


async def get_stream_client() -> httpx.AsyncClient:
    global _http_stream_client
    if _http_stream_client and not _http_stream_client.is_closed:
        return _http_stream_client
    async with _http_lock:
        if _http_stream_client and not _http_stream_client.is_closed:
            return _http_stream_client
        limits = httpx.Limits(
            max_connections=settings.http_pool_max,
            max_keepalive_connections=settings.http_pool_keepalive,
        )
        stream_timeout = httpx.Timeout(
            connect=5.0,
            read=float(settings.responses_stream_read_timeout),
            write=60.0,
            pool=60.0,
        )
        _http_stream_client = httpx.AsyncClient(limits=limits, timeout=stream_timeout, trust_env=False)
        return _http_stream_client


async def get_image_stream_client() -> httpx.AsyncClient:
    global _http_image_stream_client
    if _http_image_stream_client and not _http_image_stream_client.is_closed:
        return _http_image_stream_client
    async with _http_lock:
        if _http_image_stream_client and not _http_image_stream_client.is_closed:
            return _http_image_stream_client
        limits = httpx.Limits(
            max_connections=settings.http_pool_max,
            max_keepalive_connections=settings.http_pool_keepalive,
        )
        _http_image_stream_client = httpx.AsyncClient(
            limits=limits,
            timeout=IMAGE_UPSTREAM_TIMEOUT,
            trust_env=False,
        )
        return _http_image_stream_client


async def close_http_client() -> None:
    global _http_client, _http_stream_client, _http_image_stream_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
    if _http_stream_client and not _http_stream_client.is_closed:
        await _http_stream_client.aclose()
    _http_stream_client = None
    if _http_image_stream_client and not _http_image_stream_client.is_closed:
        await _http_image_stream_client.aclose()
    _http_image_stream_client = None


async def _post_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: Dict[str, object],
    headers: Dict[str, str],
    attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> httpx.Response:
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await client.post(url, json=json_body, headers=headers)
        except httpx.TransportError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(backoff_seconds * attempt)
    assert last_error is not None
    raise last_error


async def _send_stream_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    request = client.build_request(method, url, **kwargs)
    return await client.send(request, stream=True)


def _stream_upstream_response(
    upstream: httpx.Response,
    *,
    headers: Dict[str, str],
    media_type: str,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> StreamingResponse:
    async def iter_bytes():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            try:
                await upstream.aclose()
            finally:
                if on_close is not None:
                    await on_close()

    stream_headers = dict(headers)
    stream_headers.pop("content-length", None)
    stream_headers.setdefault("cache-control", "no-store")
    stream_headers.setdefault("x-accel-buffering", "no")
    return StreamingResponse(
        iter_bytes(),
        status_code=upstream.status_code,
        headers=stream_headers,
        media_type=media_type,
    )


class KeyCache:
    def __init__(self, ttl_seconds: int, max_size: int) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(100, int(max_size))
        self._lock = asyncio.Lock()
        self._data: Dict[str, Tuple[float, Dict[str, object]]] = {}

    async def get(self, key_hash: str) -> Optional[Dict[str, object]]:
        now = time.time()
        async with self._lock:
            item = self._data.get(key_hash)
            if not item:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._data.pop(key_hash, None)
                return None
            return value

    async def set(self, key_hash: str, value: Dict[str, object]) -> None:
        expires_at = time.time() + self._ttl
        async with self._lock:
            if len(self._data) >= self._max:
                self._data.pop(next(iter(self._data)))
            self._data[key_hash] = (expires_at, value)

    async def delete(self, key_hash: str) -> None:
        async with self._lock:
            self._data.pop(key_hash, None)


key_cache = KeyCache(settings.key_cache_ttl, settings.key_cache_max)


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _session_expires_at(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    session_days = max(1, int(settings.console_session_days or 30))
    return now + timedelta(days=session_days)


def _should_refresh_session(api_key: ApiKey, now: datetime | None = None) -> bool:
    if getattr(api_key, "kind", "api") != "session":
        return False
    expires_at = _utc_naive(getattr(api_key, "expires_at", None))
    if expires_at is None:
        return True
    now = now or datetime.utcnow()
    threshold_days = max(1, int(settings.console_session_refresh_threshold_days or 15))
    return expires_at - now <= timedelta(days=threshold_days)


def _refresh_session_if_needed(api_key: ApiKey) -> bool:
    now = datetime.utcnow()
    if not _should_refresh_session(api_key, now):
        return False
    api_key.expires_at = _session_expires_at(now)
    return True


class ResponseConversationCache:
    """Polyfill cache: stores expanded input + response output per response ID.

    Enables multi-turn conversation expansion for Responses API by replaying
    previous context when previous_response_id is referenced.
    """

    def __init__(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        max_entries: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
        max_entry_bytes: Optional[int] = None,
        max_turns: Optional[int] = None,
    ) -> None:
        self._ttl = max(1, int(ttl_seconds or settings.response_cache_ttl))
        self._max_entries = max(1, int(max_entries or settings.response_cache_max_entries))
        self._max_total_bytes = max(1024, int(max_total_bytes or settings.response_cache_max_total_bytes))
        self._max_entry_bytes = max(1024, int(max_entry_bytes or settings.response_cache_max_entry_bytes))
        self._max_turns = max(1, int(max_turns or settings.response_cache_max_turns))
        self._data: "OrderedDict[str, Tuple[float, list, list, int]]" = OrderedDict()
        self._current_bytes = 0

    @staticmethod
    def _estimate_size_bytes(expanded_input: list, response_output: list) -> int:
        payload = {"input": expanded_input, "output": response_output}
        try:
            return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            return len(repr(payload).encode("utf-8", errors="ignore"))

    def _trim_turns(self, expanded_input: list) -> list:
        max_items = max(1, self._max_turns * 2)
        if len(expanded_input) <= max_items:
            return expanded_input
        return expanded_input[-max_items:]

    def _drop(self, response_id: str) -> None:
        item = self._data.pop(response_id, None)
        if item:
            self._current_bytes = max(0, self._current_bytes - item[3])

    def _prune_expired(self, now: Optional[float] = None) -> None:
        cutoff = time.time() if now is None else now
        expired = [response_id for response_id, (expires_at, _, _, _) in self._data.items() if expires_at <= cutoff]
        for response_id in expired:
            self._drop(response_id)

    def _evict_to_budget(self) -> None:
        while self._data and (
            len(self._data) > self._max_entries or self._current_bytes > self._max_total_bytes
        ):
            oldest_response_id = next(iter(self._data))
            self._drop(oldest_response_id)

    def get(self, response_id: str) -> Optional[Tuple[list, list]]:
        item = self._data.get(response_id)
        if not item:
            return None
        expires_at, expanded_input, response_output, _size_bytes = item
        if expires_at <= time.time():
            self._drop(response_id)
            return None
        self._data.move_to_end(response_id)
        return expanded_input, response_output

    def set(self, response_id: str, expanded_input: list, response_output: list) -> None:
        now = time.time()
        self._prune_expired(now)
        trimmed_input = self._trim_turns(expanded_input)
        cached_output = _clone_responses_items(response_output)
        size_bytes = self._estimate_size_bytes(trimmed_input, cached_output)
        if size_bytes > self._max_entry_bytes:
            logger.info(
                "polyfill: skip cache for %s (%d bytes > %d budget)",
                response_id,
                size_bytes,
                self._max_entry_bytes,
            )
            self._drop(response_id)
            return
        self._drop(response_id)
        self._data[response_id] = (now + self._ttl, trimmed_input, cached_output, size_bytes)
        self._current_bytes += size_bytes
        self._data.move_to_end(response_id)
        self._evict_to_budget()


_conv_cache = ResponseConversationCache()


HOP_BY_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

IMAGE_EDIT_FILE_FIELDS = frozenset({"image", "image[]", "mask", "mask[]"})


def filter_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


def extract_upstream_request_id(headers) -> str:
    for key, value in (headers or {}).items():
        if key.lower() in {"x-request-id", "request-id", "apim-request-id", "x-ms-request-id"}:
            return str(value or "").strip()
    return ""


def _build_upstream_headers(cfg) -> Dict[str, str]:
    """Build upstream request headers with auth style from ModelConfig."""
    headers = {"content-type": "application/json"}
    if getattr(cfg, "auth_style", "azure") == "bearer":
        headers["authorization"] = f"Bearer {cfg.api_key}"
    else:
        headers["api-key"] = cfg.api_key
    return headers


def _channel_usage_kwargs(cfg, cpa_channel=None) -> Dict[str, object]:
    if cpa_channel is not None:
        return {
            "channel_id": getattr(cpa_channel, "channel_id", "") or getattr(cfg, "channel_id", ""),
            "channel_type": getattr(cfg, "channel_type", "") or "account_pool",
            "provider_platform": getattr(cfg, "provider_platform", "") or "cpa_gemini",
            "provider_account_fingerprint": getattr(cfg, "provider_account_fingerprint", ""),
            "fallback_from_channel_id": getattr(cfg, "fallback_from_channel_id", ""),
            "route_attempt": getattr(cfg, "route_attempt", 0),
        }
    return {
        "channel_id": getattr(cfg, "channel_id", ""),
        "channel_type": getattr(cfg, "channel_type", ""),
        "provider_platform": getattr(cfg, "provider_platform", ""),
        "provider_account_fingerprint": getattr(cfg, "provider_account_fingerprint", ""),
        "fallback_from_channel_id": getattr(cfg, "fallback_from_channel_id", ""),
        "route_attempt": getattr(cfg, "route_attempt", 0),
    }


def _record_channel_success(cfg, *, duration_ms: int = 0) -> None:
    channel_router.record_success(getattr(cfg, "channel_id", ""), latency_ms=duration_ms)


def _record_channel_failure(cfg, *, status_code: int | None = None, error_code: str = "") -> None:
    channel_id = getattr(cfg, "channel_id", "")
    if not channel_id:
        return
    if status_code is None or should_record_channel_failure(int(status_code or 0)):
        channel_router.record_failure(channel_id, error_code=error_code or str(status_code or "request_error"))


def _channel_fallback_config(previous_cfg, fallback_cfg):
    previous_channel_id = getattr(previous_cfg, "channel_id", "") or ""
    if not previous_channel_id:
        return fallback_cfg
    try:
        return replace(
            fallback_cfg,
            fallback_from_channel_id=previous_channel_id,
            route_attempt=int(getattr(previous_cfg, "route_attempt", 0) or 0) + 1,
        )
    except Exception:
        return fallback_cfg


def _channel_attempted_ids(cfg) -> Tuple[str, ...]:
    values = (
        getattr(cfg, "fallback_from_channel_id", "") or "",
        getattr(cfg, "channel_id", "") or "",
    )
    return tuple(dict.fromkeys(item for item in values if item))


def _next_channel_fallback_config(public_model, previous_cfg, endpoint: str, *, reason: str):
    previous_channel_id = getattr(previous_cfg, "channel_id", "") or ""
    if not previous_channel_id or str(previous_channel_id).startswith("system:"):
        return None
    attempted = _channel_attempted_ids(previous_cfg)
    if len(attempted) >= CHANNEL_FALLBACK_MAX_ATTEMPTS:
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
        "provider channel fallback endpoint=%s from=%s to=%s reason=%s attempt=%s",
        endpoint,
        previous_channel_id,
        fallback_cfg.channel_id,
        reason,
        fallback_cfg.route_attempt,
    )
    return fallback_cfg


def _system_fallback_config(
    public_model,
    previous_cfg,
    endpoint: str,
    messages: Optional[List[dict]] = None,
    tools: Optional[list] = None,
    *,
    lock_model_selection: bool = False,
    reason: str = "",
):
    previous_channel_id = getattr(previous_cfg, "channel_id", "") or ""
    if not previous_channel_id or str(previous_channel_id).startswith("system:"):
        return None
    resolved = model_registry.resolve_system_fallback(
        public_model,
        previous_cfg,
        endpoint,
        messages,
        tools,
        lock_model_selection=lock_model_selection,
    )
    if resolved is None:
        return None
    logger.warning(
        "provider channel system fallback endpoint=%s from=%s to=%s reason=%s attempt=%s",
        endpoint,
        previous_channel_id,
        resolved.backend.provider_platform or resolved.backend.channel_type or "catalog",
        reason,
        resolved.backend.route_attempt,
    )
    return resolved.backend


def _next_provider_or_system_fallback_config(
    public_model,
    previous_cfg,
    endpoint: str,
    messages: Optional[List[dict]] = None,
    tools: Optional[list] = None,
    *,
    lock_model_selection: bool = False,
    reason: str = "",
) -> Tuple[Optional[Any], str]:
    channel_fallback_cfg = _next_channel_fallback_config(
        public_model,
        previous_cfg,
        endpoint,
        reason=reason,
    )
    if channel_fallback_cfg is not None:
        return channel_fallback_cfg, _channel_fallback_route_reason(reason)

    system_fallback_cfg = _system_fallback_config(
        public_model,
        previous_cfg,
        endpoint,
        messages,
        tools,
        lock_model_selection=lock_model_selection,
        reason=reason,
    )
    if system_fallback_cfg is not None:
        return system_fallback_cfg, _system_fallback_route_reason(reason)
    return None, ""


def _channel_fallback_route_reason(reason: str) -> str:
    return f"channel_fallback:{str(reason or 'retry')[:40]}"


def _system_fallback_route_reason(reason: str) -> str:
    return f"system_fallback:{str(reason or 'retry')[:39]}"


def _should_try_channel_fallback(cfg, *, status_code: int | None = None, error_code: str = "") -> bool:
    channel_id = getattr(cfg, "channel_id", "") or ""
    if not channel_id or str(channel_id).startswith("system:"):
        return False
    if error_code and error_code in CHANNEL_FALLBACK_RETRY_ERRORS:
        return True
    if status_code is not None and should_record_channel_failure(int(status_code or 0)):
        return True
    return False


def _requested_image_count_from_pairs(pairs: List[Tuple[str, str]]) -> int:
    for key, value in reversed(pairs):
        if key != "n":
            continue
        try:
            return max(1, int(value or "1"))
        except (TypeError, ValueError):
            break
    return 1


def _requested_image_count_from_json(payload: Dict[str, object]) -> int:
    try:
        return max(1, int(payload.get("n") or 1))
    except (AttributeError, TypeError, ValueError):
        return 1


def _vertex_image_candidate_count_error() -> JSONResponse:
    return _openai_error_response(
        "Gemini image requests currently support only n=1.",
        code="image_candidate_count_not_supported",
        param="n",
        status_code=400,
    )


def _unsupported_google_image_lane_error(delivery_lane: str) -> JSONResponse:
    lane = (delivery_lane or "").strip() or "unknown"
    return _openai_error_response(
        f"Unsupported Gemini image delivery lane: {lane}.",
        error_type="server_error",
        code="unsupported_image_delivery_lane",
        param="model",
        status_code=503,
    )


def _image_job_required_error(image_count: int) -> JSONResponse:
    sync_limit = max(1, int(settings.image_job_sync_input_limit or 2))
    async_limit = max(sync_limit, int(settings.image_job_async_max_inputs or sync_limit))
    return _openai_error_response(
        (
            f"Gemini image edits with more than {sync_limit} input images must use "
            f"POST /v1/image-jobs/edits. Received {image_count} images; this deployment "
            f"currently supports up to {async_limit} async input images."
        ),
        code="image_job_required",
        param="image",
        status_code=400,
    )


def _image_job_timeout_error(image_count: int) -> JSONResponse:
    async_limit = max(1, int(settings.image_job_async_max_inputs or 8))
    return _openai_error_response(
        (
            "Gemini image edit exceeded the sync response budget on this deployment. "
            "Retry via POST /v1/image-jobs/edits for a more reliable async workflow. "
            f"Received {image_count} image(s); this deployment currently supports up to "
            f"{async_limit} async input images."
        ),
        code="image_job_required",
        param="image",
        status_code=400,
    )


def _map_image_size_to_aspect_ratio(size: str) -> str:
    aspect_ratio_map = {
        "1024x1024": "1:1",
        "1792x1024": "16:9",
        "1024x1792": "9:16",
        "1280x896": "4:3",
        "896x1280": "3:4",
    }
    return aspect_ratio_map.get(size, "1:1")


def _encode_multipart_form_data(
    form_fields: List[Tuple[str, str]],
    file_fields: List[Tuple[str, Tuple[str, bytes, str]]],
) -> Tuple[bytes, str]:
    boundary = f"coincoin-{secrets.token_hex(16)}"
    body = bytearray()

    def _append_text_part(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend((value or "").encode("utf-8"))
        body.extend(b"\r\n")

    def _append_file_part(name: str, filename: str, content: bytes, content_type: str) -> None:
        safe_filename = filename or "upload.bin"
        safe_content_type = content_type or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"; filename="{safe_filename}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {safe_content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")

    for key, value in form_fields:
        _append_text_part(str(key), str(value))

    for key, (filename, content, content_type) in file_fields:
        _append_file_part(str(key), str(filename), content, str(content_type))

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


async def _parse_image_edit_form(
    request: Request,
) -> Tuple[str, List[Tuple[str, str]], List[Tuple[str, Tuple[str, bytes, str]]]]:
    form = await request.form()

    requested_model = ""
    scalar_fields: List[Tuple[str, str]] = []
    file_fields: List[Tuple[str, Tuple[str, bytes, str]]] = []

    for key, value in form.multi_items():
        if key == "model_provider":
            continue
        if isinstance(value, UploadFile):
            if key not in IMAGE_EDIT_FILE_FIELDS:
                continue
            filename = value.filename or "upload.bin"
            content_type = value.content_type or "application/octet-stream"
            file_fields.append((key, (filename, await value.read(), content_type)))
            continue

        text_value = "" if value is None else str(value)
        if key == "model":
            if text_value.strip():
                requested_model = text_value.strip()
            continue

        scalar_fields.append((key, text_value))

    return requested_model, scalar_fields, file_fields


def _build_vertex_image_edit_payload(
    form_fields: List[Tuple[str, str]],
    file_fields: List[Tuple[str, Tuple[str, bytes, str]]],
) -> Dict[str, object]:
    prompt = ""
    size = ""
    requested_image_count = _requested_image_count_from_pairs(form_fields)
    image_parts = []

    for key, value in form_fields:
        if key == "prompt":
            prompt = value
        elif key == "size":
            size = value

    for key, (_, content, content_type) in file_fields:
        if key not in {"image", "image[]"}:
            continue
        image_parts.append(
            {
                "inlineData": {
                    "mimeType": content_type or "application/octet-stream",
                    "data": base64.b64encode(content).decode("utf-8"),
                }
            }
        )

    if not image_parts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="image file is required")

    parts = list(image_parts)
    if prompt:
        parts.append({"text": prompt})

    generation_config: Dict[str, object] = {
        "responseModalities": ["IMAGE"],
        "candidateCount": requested_image_count,
    }
    if size:
        generation_config["imageConfig"] = {
            "aspectRatio": _map_image_size_to_aspect_ratio(size),
        }

    return {
        "contents": [{
            "role": "user",
            "parts": parts,
        }],
        "generationConfig": generation_config,
    }


def _build_vertex_image_generation_payload(payload: Dict[str, object]) -> Dict[str, object]:
    prompt = str(payload.get("prompt") or "").strip()
    size = str(payload.get("size") or "").strip()
    requested_image_count = _requested_image_count_from_json(payload)

    parts = []
    if prompt:
        parts.append({"text": prompt})

    generation_config: Dict[str, object] = {
        "responseModalities": ["IMAGE"],
        "candidateCount": requested_image_count,
    }
    if size:
        generation_config["imageConfig"] = {
            "aspectRatio": _map_image_size_to_aspect_ratio(size),
        }

    return {
        "contents": [{
            "role": "user",
            "parts": parts,
        }],
        "generationConfig": generation_config,
    }


def _translate_vertex_image_response(data: Dict[str, object]) -> Dict[str, object]:
    output_images = []
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or {}
            if not isinstance(inline, dict):
                continue
            image_b64 = inline.get("data")
            if image_b64:
                output_images.append({"b64_json": image_b64})

    return {
        "created": int(time.time()),
        "data": output_images,
    }


def _is_cpa_gemini_lane(public_model) -> bool:
    return (getattr(public_model, "delivery_lane", "") or "").strip().lower() == gemini_cpa.DELIVERY_LANE


def _gemini_cpa_empty_image_error() -> JSONResponse:
    return _openai_error_response(
        "Gemini CPA returned no output images.",
        error_type="server_error",
        code="empty_image_result",
        status_code=502,
    )


def _openai_error_response(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    code: Optional[str] = None,
    param: Optional[str] = None,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
    )


def _notify_fallback_exhausted(
    *,
    endpoint: str,
    model: str,
    status_code: int,
    reason: str,
    cfg,
    route_reason: str = "",
    upstream_request_id: str = "",
) -> bool:
    return notify_fallback_exhausted(
        FallbackExhaustedAlert(
            endpoint=endpoint,
            model=model,
            status_code=int(status_code or 0),
            reason=str(reason or ""),
            route_reason=str(route_reason or ""),
            channel_id=str(getattr(cfg, "channel_id", "") or ""),
            fallback_from_channel_id=str(getattr(cfg, "fallback_from_channel_id", "") or ""),
            route_attempt=int(getattr(cfg, "route_attempt", 0) or 0),
            provider_platform=str(getattr(cfg, "provider_platform", "") or ""),
            channel_type=str(getattr(cfg, "channel_type", "") or ""),
            upstream_request_id=str(upstream_request_id or ""),
        )
    )


def _model_resolution_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, UnknownModelError):
        return _openai_error_response(str(exc), code="model_not_found", param="model", status_code=400)
    if isinstance(exc, ModelCapabilityError):
        return _openai_error_response(str(exc), code="model_capability_mismatch", param="model", status_code=400)
    return _openai_error_response("Unable to resolve model", error_type="server_error", code="model_resolution_failed", status_code=500)


@router.get("/responses")
async def responses_health():
    return {"status": "ok"}


async def _resolve_user(request: Request, db: AsyncSession):
    """Resolve API key → user object. Identity + active + expiry check."""
    model_registry.ensure_initialized()
    if not model_registry.has_routable_models():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="no routable models configured")

    client_key = extract_api_key(request)
    if not client_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing api key")

    key_hash = hash_key(client_key)
    cached = await key_cache.get(key_hash)
    if cached:
        try:
            result = await db.execute(select(User).where(User.id == cached["id"]))
            user = result.scalar_one_or_none()
        except Exception:
            logger.exception("db lookup failed")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error")
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
        key_kind = str(cached.get(_KEY_KIND_ATTR) or "api")
        key_id = str(cached.get(_KEY_ID_ATTR) or "")
        key_controls = cached.get("controls", {}) if isinstance(cached.get("controls", {}), dict) else {}
        station_context = cached.get("station_context", {}) if isinstance(cached.get("station_context", {}), dict) else {}
        session_refreshed = False
    else:
        try:
            result = await db.execute(
                select(ApiKey)
                .where(ApiKey.key_hash == key_hash, ApiKey.status == "active")
                .options(selectinload(ApiKey.user))
            )
            api_key = result.scalar_one_or_none()
        except Exception:
            logger.exception("db lookup failed")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error")
        if not api_key or not api_key.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")

        if _utc_naive(getattr(api_key, "expires_at", None)) and _utc_naive(api_key.expires_at) < datetime.utcnow():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired, please login again")

        user = api_key.user
        key_id = api_key.id
        key_kind = getattr(api_key, "kind", None) or "api"
        session_refreshed = _refresh_session_if_needed(api_key)
        key_controls = {
            "monthly_quota_cents": getattr(api_key, "monthly_quota_cents", None),
            "total_quota_cents": getattr(api_key, "total_quota_cents", None),
            "ip_allowlist": getattr(api_key, "ip_allowlist", None),
            "expires_at": getattr(api_key, "expires_at", None),
        }
        try:
            station_context = await station_context_for_user(db, user.id)
        except Exception:
            logger.exception("station context lookup failed")
            station_context = {}
        await key_cache.set(
            key_hash,
            {
                "id": user.id,
                _KEY_ID_ATTR: key_id,
                _KEY_KIND_ATTR: key_kind,
                "controls": key_controls,
                "station_context": station_context,
                "session_refreshed": session_refreshed,
            },
        )
    setattr(user, _KEY_KIND_ATTR, key_kind)
    setattr(user, _KEY_ID_ATTR, key_id)
    setattr(user, "_api_key_controls", key_controls)
    setattr(user, "_station_context", station_context)
    setattr(user, "_session_refreshed", session_refreshed)
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user blocked")
    if getattr(user, "_session_refreshed", False):
        await db.commit()
    return user


async def _mark_api_key_used(db: AsyncSession, user: User) -> None:
    key_id = getattr(user, _KEY_ID_ATTR, "")
    if not key_id or getattr(user, _KEY_KIND_ATTR, "api") != "api":
        return
    try:
        await db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.user_id == user.id, ApiKey.kind == "api")
            .values(last_used_at=datetime.utcnow())
        )
        await db.commit()
    except Exception:
        logger.exception("failed to update api key last_used_at")
        await db.rollback()


async def _enforce_api_key_controls(request: Request, db: AsyncSession, user: User) -> None:
    key_id = getattr(user, _KEY_ID_ATTR, "")
    if not key_id or getattr(user, _KEY_KIND_ATTR, "api") != "api":
        return

    key_controls = getattr(user, "_api_key_controls", {}) or {}
    if not key_controls:
        return

    expires_at = key_controls.get("expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expires_at.tzinfo is not None:
                    expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                expires_at = None
        if isinstance(expires_at, datetime) and expires_at < datetime.utcnow():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="api key expired")

    allowlist = _parse_ip_allowlist(key_controls.get("ip_allowlist"))
    if allowlist and not _ip_allowed(_client_ip(request), allowlist):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="api key ip not allowed")

    monthly_quota = key_controls.get("monthly_quota_cents")
    total_quota = key_controls.get("total_quota_cents")
    if not monthly_quota and not total_quota:
        return

    try:
        pending_cost = await usage_buffer.get_pending_cost_for_api_key(key_id)
        if total_quota:
            total_used = (
                await db.execute(
                    select(func.coalesce(func.sum(RequestLog.cost_cents), 0)).where(RequestLog.api_key_id == key_id)
                )
            ).scalar() or 0
            if int(total_used) + int(pending_cost) >= int(total_quota):
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="api key total quota exceeded")
        if monthly_quota:
            monthly_used = (
                await db.execute(
                    select(func.coalesce(func.sum(RequestLog.cost_cents), 0)).where(
                        RequestLog.api_key_id == key_id,
                        RequestLog.created_at >= _month_start_utc(),
                    )
                )
            ).scalar() or 0
            if int(monthly_used) + int(pending_cost) >= int(monthly_quota):
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="api key monthly quota exceeded")
    except HTTPException:
        raise
    except Exception:
        logger.exception("api key quota lookup failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error")


async def authenticate_user(request: Request, db: AsyncSession):
    """Light auth: identity + active check only. No balance/quota enforcement.
    Use for payment, redeem, balance queries — endpoints where zero balance is fine."""
    return await _resolve_user(request, db)


async def authorize_request(request: Request, db: AsyncSession):
    """Full auth: identity + active + rate limit + balance/quota checks.
    Use for API proxy endpoints that consume resources.
    Only kind='api' keys are allowed here; session keys get 403."""
    user = await _resolve_user(request, db)

    if getattr(user, _KEY_KIND_ATTR, "api") != "api":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="please generate an API key from your dashboard",
        )

    await _enforce_api_key_controls(request, db, user)

    if user.request_limit_per_minute is not None:
        allowed = await rate_limiter.allow(user.id, int(user.request_limit_per_minute))
        if not allowed:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")

    # Token 限制检查（兼容旧逻辑）
    pending_tokens = await usage_buffer.get_pending_tokens(user.id)
    if user.token_limit is not None and (user.token_used + pending_tokens) >= user.token_limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="token limit exceeded")
    
    # 余额检查（balance 计费模式）
    if settings.billing_mode == "balance":
        pending_cost = await usage_buffer.get_pending_cost(user.id)
        from .billing import get_available_balance_cents
        available = await get_available_balance_cents(db, user, pending_cost_cents=pending_cost)
        if int(available.get("available_cents", 0)) <= 0:
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient balance")

    if user.request_limit_per_day is not None:
        today = china_today()
        try:
            result = await db.execute(
                select(UsageDaily.requests_total).where(
                    UsageDaily.user_id == user.id, UsageDaily.day == today
                )
            )
            row = result.first()
            used_today = int(row[0]) if row else 0
            pending_requests = await usage_buffer.get_pending_requests_today(user.id)
            if (used_today + pending_requests) >= int(user.request_limit_per_day):
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="daily request limit exceeded")
        except HTTPException:
            raise
        except Exception:
            logger.exception("daily limit lookup failed")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error")

    await _mark_api_key_used(db, user)

    return user


@router.post("/responses")
async def proxy_responses(request: Request, db: AsyncSession = Depends(get_db)):
    user = await authorize_request(request, db)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payload must be a json object")

    requested_model = str(payload.get("model") or "").strip()
    messages_for_route, tools_for_route = extract_messages_for_routing_from_responses_payload(payload)
    try:
        station_model = await resolve_station_model_for_user(
            db,
            user,
            requested_model,
            "responses",
            messages_for_route,
            tools_for_route,
        )
        resolved_model = station_model.resolved_model if station_model else model_registry.resolve_public_model(
            requested_model,
            "responses",
            messages_for_route,
            tools_for_route,
        )
    except Exception as exc:
        return _model_resolution_error_response(exc)
    public_model = resolved_model.public_model
    display_model = station_model.display_model if station_model else public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason
    api_key_id = getattr(user, _KEY_ID_ATTR, "")
    price_input_per_million = station_model.retail_input_per_million if station_model else public_model.price_input_per_million
    price_output_per_million = station_model.retail_output_per_million if station_model else public_model.price_output_per_million

    if public_model.delivery_lane == CLAUDE_COMPAT_PROVIDER_KIRO_GO:
        chat_payload: Dict[str, Any] = {
            "model": used_cfg.model_id,
            "messages": _responses_input_to_chat_messages(payload.get("input")),
            "stream": bool(payload.get("stream")),
        }
        if not chat_payload["messages"]:
            chat_payload["messages"] = [{"role": "user", "content": " "}]
        if isinstance(payload.get("instructions"), str) and payload["instructions"].strip():
            chat_payload["messages"] = [{"role": "system", "content": payload["instructions"].strip()}] + chat_payload["messages"]
        tools = _responses_tools_to_chat_tools(payload.get("tools"))
        if tools:
            chat_payload["tools"] = tools
        if "max_output_tokens" in payload:
            chat_payload["max_tokens"] = payload.get("max_output_tokens")
        elif "max_tokens" in payload:
            chat_payload["max_tokens"] = payload.get("max_tokens")
        for field in ("temperature", "top_p", "stop", "tool_choice"):
            if field in payload:
                chat_payload[field] = payload[field]

        headers = _build_upstream_headers(used_cfg)
        upstream_url = f"{_normalize_openai_base_url(used_cfg.upstream_url)}/chat/completions"
        if chat_payload.get("stream"):
            stream_client = await get_stream_client()
            try:
                req = stream_client.build_request("POST", upstream_url, json=chat_payload, headers=headers)
                upstream = await stream_client.send(req, stream=True)
            except (httpx.TimeoutException, httpx.RequestError):
                _record_channel_failure(used_cfg, error_code="upstream_unreachable")
                return JSONResponse(
                    content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                    status_code=502,
                )
            stream_headers = filter_headers(dict(upstream.headers))
            stream_headers.pop("content-length", None)
            stream_headers.setdefault("cache-control", "no-cache")
            stream_headers.setdefault("x-accel-buffering", "no")
            stream_headers["content-type"] = "text/event-stream; charset=utf-8"
            stream_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
            response_id = ""
            output_items: List[Dict[str, Any]] = []
            text_parts: List[str] = []
            current_tool_call_id = ""
            current_tool_name = ""
            current_tool_arguments = ""
            tool_counter = 0
            stream_t0 = time.monotonic()

            async def iter_events():
                nonlocal response_id, current_tool_call_id, current_tool_name, current_tool_arguments, tool_counter
                try:
                    response_id = f"resp_{secrets.token_hex(20)}"
                    created_at = int(time.time())
                    yield _responses_sse_line("response.created", {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created_at,
                            "status": "in_progress",
                            "model": display_model,
                            "output": [],
                        },
                    })
                    async for line in upstream.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                        except Exception:
                            continue
                        if not isinstance(event, dict):
                            continue
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            stream_usage["input"] = extract_total_input_tokens(usage)
                            stream_usage["output"] = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                            stream_usage["cache_read"] = extract_cache_read_tokens(usage)
                            stream_usage["cache_creation"] = extract_cache_creation_tokens(usage)
                        if isinstance(event.get("error"), dict):
                            yield _responses_sse_line("response.failed", {
                                "type": "response.failed",
                                "response": {
                                    "id": response_id,
                                    "object": "response",
                                    "created_at": created_at,
                                    "status": "failed",
                                    "model": display_model,
                                    "error": event["error"],
                                },
                            })
                            yield "data: [DONE]\n\n"
                            return

                        choices = event.get("choices")
                        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                        if not response_id:
                            response_id = str(event.get("id") or f"resp_{secrets.token_hex(20)}")

                        if isinstance(delta.get("content"), str) and delta["content"]:
                            text = delta["content"]
                            text_parts.append(text)
                            yield _responses_sse_line("response.output_text.delta", {
                                "type": "response.output_text.delta",
                                "response_id": response_id,
                                "delta": text,
                            })

                        tool_calls = delta.get("tool_calls")
                        if isinstance(tool_calls, list):
                            for tool_call in tool_calls:
                                if not isinstance(tool_call, dict):
                                    continue
                                tool_index = int(tool_call.get("index") or tool_counter)
                                tool_counter = max(tool_counter, tool_index + 1)
                                if tool_call.get("id"):
                                    current_tool_call_id = str(tool_call.get("id"))
                                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                                if function.get("name"):
                                    current_tool_name = str(function.get("name"))
                                    current_tool_arguments = ""
                                    yield _responses_sse_line("response.output_item.added", {
                                        "type": "response.output_item.added",
                                        "response_id": response_id,
                                        "item": {
                                            "type": "function_call",
                                            "id": current_tool_call_id or f"call_{secrets.token_hex(8)}",
                                            "call_id": current_tool_call_id or f"call_{secrets.token_hex(8)}",
                                            "name": current_tool_name,
                                            "arguments": "",
                                        },
                                    })
                                if isinstance(function.get("arguments"), str) and function["arguments"]:
                                    current_tool_arguments += function["arguments"]
                                    yield _responses_sse_line("response.function_call_arguments.delta", {
                                        "type": "response.function_call_arguments.delta",
                                        "response_id": response_id,
                                        "delta": {"arguments": function["arguments"]},
                                    })

                        finish_reason = choice.get("finish_reason")
                        if finish_reason == "tool_calls" and current_tool_name:
                            item = {
                                "type": "function_call",
                                "id": current_tool_call_id or f"call_{secrets.token_hex(8)}",
                                "call_id": current_tool_call_id or f"call_{secrets.token_hex(8)}",
                                "name": current_tool_name,
                                "arguments": current_tool_arguments,
                            }
                            output_items.append(item)
                            yield _responses_sse_line("response.function_call_arguments.done", {
                                "type": "response.function_call_arguments.done",
                                "response_id": response_id,
                                "arguments": current_tool_arguments,
                                "item": item,
                            })
                            current_tool_call_id = ""
                            current_tool_name = ""
                            current_tool_arguments = ""

                    if text_parts:
                        output_items.append(_build_responses_output_text_item("".join(text_parts)))
                    usage_payload = {
                        "input_tokens": stream_usage["input"],
                        "output_tokens": stream_usage["output"],
                        "total_tokens": stream_usage["input"] + stream_usage["output"],
                    }
                    if stream_usage["cache_read"]:
                        usage_payload["input_tokens_details"] = {"cached_tokens": stream_usage["cache_read"]}
                    completed = {
                        "id": response_id,
                        "object": "response",
                        "created_at": created_at,
                        "status": "completed",
                        "model": display_model,
                        "output": output_items,
                        "output_text": "".join(text_parts),
                        "usage": usage_payload,
                    }
                    yield _responses_sse_line("response.completed", {
                        "type": "response.completed",
                        "response": completed,
                    })
                    yield "data: [DONE]\n\n"
                finally:
                    await upstream.aclose()
                    if upstream.status_code < 400:
                        _record_channel_success(used_cfg, duration_ms=int((time.monotonic() - stream_t0) * 1000))
                        if response_id and output_items:
                            _conv_cache.set(response_id, _normalize_responses_input_items(payload.get("input")), output_items)
                        dur = int((time.monotonic() - stream_t0) * 1000)
                        asyncio.create_task(usage_buffer.add(
                            user.id,
                            api_key_id=api_key_id,
                            input_tokens=stream_usage["input"],
                            output_tokens=stream_usage["output"],
                            cache_read_tokens=stream_usage["cache_read"],
                            cache_creation_tokens=stream_usage["cache_creation"],
                            requests=1,
                            endpoint="responses:stream",
                            model=display_model,
                            customer_model_alias=display_model,
                            provider_model=public_model.provider_model or used_cfg.model_id,
                            route_reason=used_route_reason,
                            duration_ms=dur,
                            status_code=upstream.status_code,
                            price_input_per_million=price_input_per_million,
                            price_output_per_million=price_output_per_million,
                            usage_unit_type="tokens",
                            billable_sku=public_model.billable_sku or display_model,
                            upstream_request_id=extract_upstream_request_id(upstream.headers),
                            **_channel_usage_kwargs(used_cfg),
                            **usage_pricing_kwargs(public_model, station_model),
                        ))
                    else:
                        _record_channel_failure(used_cfg, status_code=upstream.status_code)

            return StreamingResponse(
                iter_events(),
                status_code=upstream.status_code,
                headers=stream_headers,
                media_type="text/event-stream",
            )

        client = await get_http_client()
        t0 = time.monotonic()
        try:
            upstream = await client.post(upstream_url, json=chat_payload, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError):
            _record_channel_failure(used_cfg, error_code="upstream_unreachable")
            return JSONResponse(
                content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                status_code=502,
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        content_type = upstream.headers.get("content-type", "application/json")
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        if "application/json" in content_type:
            try:
                data = upstream.json()
            except Exception:
                _record_channel_failure(used_cfg, error_code="upstream_invalid_json")
                return JSONResponse(
                    content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                    status_code=502,
                    headers=response_headers,
                )
        else:
            data = upstream.text

        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            _record_channel_failure(used_cfg, status_code=upstream.status_code, error_code=str(upstream.status_code))
            return JSONResponse(
                content={"error": data["error"]},
                status_code=upstream.status_code if upstream.status_code >= 400 else 502,
                headers=response_headers,
            )
        if upstream.status_code >= 400:
            _record_channel_failure(used_cfg, status_code=upstream.status_code)
            return JSONResponse(
                content={"error": {"message": str(data)[:500] if data else "upstream error", "type": "upstream_error", "code": str(upstream.status_code)}},
                status_code=upstream.status_code,
                headers=response_headers,
            )
        if not isinstance(data, dict):
            _record_channel_success(used_cfg, duration_ms=duration_ms)
            return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)

        response_payload = _translate_chat_response_to_responses(data, display_model)
        usage = response_payload.get("usage") or {}
        input_tokens_delta = extract_total_input_tokens(usage)
        output_tokens_delta = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cache_read_tokens_delta = extract_cache_read_tokens(usage)
        cache_creation_tokens_delta = extract_cache_creation_tokens(usage)
        response_id = response_payload.get("id")
        response_output = response_payload.get("output")
        if response_id and isinstance(response_output, list):
            _conv_cache.set(response_id, _normalize_responses_input_items(payload.get("input")), response_output)
        _record_channel_success(used_cfg, duration_ms=duration_ms)
        await usage_buffer.add(
            user.id,
            api_key_id=api_key_id,
            input_tokens=input_tokens_delta,
            output_tokens=output_tokens_delta,
            cache_read_tokens=cache_read_tokens_delta,
            cache_creation_tokens=cache_creation_tokens_delta,
            requests=1,
            endpoint="responses",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            usage_unit_type="tokens",
            billable_sku=public_model.billable_sku or display_model,
            upstream_request_id=upstream_request_id,
            **_channel_usage_kwargs(used_cfg),
            **usage_pricing_kwargs(public_model, station_model),
        )
        return JSONResponse(content=response_payload, status_code=upstream.status_code, headers=response_headers)

    payload["model"] = used_cfg.model_id
    payload.pop("model_provider", None)
    _sanitize_encrypted_ids(payload)
    _ensure_content_text(payload)
    prompt_cache_key = build_claude_code_prompt_cache_key(user, api_key_id, display_model, public_model)
    if prompt_cache_key:
        payload["prompt_cache_key"] = prompt_cache_key

    _text = payload.get("text")
    if isinstance(_text, dict) and "verbosity" in _text:
        _text["verbosity"] = "medium"

    if public_model.routing_mode == "legacy_auto" or used_cfg.strip_unsupported:
        if payload.pop("context_management", None) is not None:
            logger.info("responses compat: dropped context_management for legacy upstream")

    _prev_resp_id = payload.get("previous_response_id")
    if _prev_resp_id:
        _cached_conv = _conv_cache.get(_prev_resp_id)
        if _cached_conv:
            _expanded_counts = _apply_previous_response_polyfill(payload, _cached_conv)
            logger.info("polyfill: expanded from %s (%d+%d+%d items) and cleared previous_response_id",
                        _prev_resp_id, *_expanded_counts)
        else:
            payload.pop("previous_response_id", None)
            logger.warning("polyfill: %s not in cache, dropping previous_response_id and sending current input only", _prev_resp_id)

    _input = payload.get("input")
    if isinstance(_input, list):
        cleaned = []
        for item in _input:
            if isinstance(item, dict):
                itype = item.get("type")
                if itype == "item_reference":
                    continue
                if itype is None or itype == "":
                    if "role" in item or "content" in item:
                        item["type"] = "message"
                    else:
                        continue
                item.pop("id", None)
            cleaned.append(item)
        payload["input"] = cleaned

    _expanded_input = _normalize_responses_input_items(payload.get("input"))

    payload["store"] = True

    base_payload = dict(payload)

    def _uses_gemini_cpa(cfg) -> bool:
        if public_model.delivery_lane != gemini_cpa.DELIVERY_LANE:
            return False
        channel_id = str(getattr(cfg, "channel_id", "") or "").strip()
        channel_type = str(getattr(cfg, "channel_type", "") or "").strip()
        provider_platform = str(getattr(cfg, "provider_platform", "") or "").strip()
        if not channel_id:
            return True
        return channel_type == "account_pool" or provider_platform == "cpa_gemini"

    def _select_gemini_cpa_channel(cfg):
        if not _uses_gemini_cpa(cfg):
            return None
        return gemini_cpa.select_channel(public_model, cfg)

    upstream_url = _build_openai_responses_upstream_url(used_cfg.upstream_url)
    cpa_channel = None

    if _uses_gemini_cpa(used_cfg):
        try:
            cpa_channel = _select_gemini_cpa_channel(used_cfg)
        except gemini_cpa.GeminiCpaChannelUnavailable as exc:
            return _openai_error_response(
                str(exc),
                error_type="server_error",
                code="gemini_cpa_channel_cooling_down",
                status_code=503,
            )
        if cpa_channel is not None:
            base_payload["model"] = cpa_channel.provider_model
            upstream_url = gemini_cpa.chat_completions_url(cpa_channel)

    _STRIP_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty",
                     "max_output_tokens", "n", "logprobs", "top_logprobs", "seed")

    if base_payload.get("stream"):
        model_registry.ensure_initialized()
        fallback_cfg = model_registry.models.get("fallback") or model_registry.get("premium")
        cheap_cfg = model_registry.models.get("cheap")
        allow_fallback = public_model.routing_mode == "legacy_auto"
        is_cheap = bool(allow_fallback and cheap_cfg and used_cfg.model_id == cheap_cfg.model_id)
        can_fallback = allow_fallback and (
            (used_cfg.upstream_url != fallback_cfg.upstream_url) or (used_cfg.model_id != fallback_cfg.model_id)
        )
        if resolved_model.lock_model_selection and fallback_cfg.model_id != used_cfg.model_id:
            can_fallback = False
        stream_client = await get_stream_client()

        async def _send_stream(cfg, *, is_fallback=False):
            nonlocal cpa_channel
            cpa_channel = _select_gemini_cpa_channel(cfg)
            send_payload = dict(base_payload)
            send_payload["model"] = cpa_channel.provider_model if cpa_channel is not None else cfg.model_id
            if is_fallback:
                send_payload.pop("previous_response_id", None)
            send_payload["store"] = True
            if "cognitiveservices.azure.com" in (cfg.upstream_url or ""):
                if "codex" not in (cfg.model_id or "").lower():
                    send_payload.pop("reasoning", None)
            if cfg.strip_unsupported:
                for param in _STRIP_PARAMS:
                    send_payload.pop(param, None)
            req_url = gemini_cpa.responses_url(cpa_channel) if cpa_channel is not None else _build_openai_responses_upstream_url(cfg.upstream_url)
            req_headers = gemini_cpa.build_headers(cpa_channel) if cpa_channel is not None else _build_upstream_headers(cfg)
            logger.info("stream → %s  model=%s  store=%s  has_prev_resp=%s  input_types=%s",
                        req_url, send_payload.get("model"), send_payload.get("store"),
                        "previous_response_id" in send_payload,
                        [i.get("type") for i in send_payload.get("input", []) if isinstance(i, dict)])
            req = stream_client.build_request("POST", req_url, json=send_payload, headers=req_headers)
            return await stream_client.send(req, stream=True)

        async def _close_stream_for_retry(current_upstream) -> bytes:
            body = b""
            try:
                body = await current_upstream.aread()
            except Exception:
                pass
            try:
                await current_upstream.aclose()
            except Exception:
                pass
            return body

        async def _retry_stream_with_cfg(next_cfg, route_reason: str, failure_reason: str):
            nonlocal can_fallback, is_cheap, used_cfg, used_route_reason
            used_cfg = next_cfg
            used_route_reason = route_reason
            if str(getattr(used_cfg, "channel_id", "") or "").startswith("system:"):
                can_fallback = False
                is_cheap = False
            try:
                return await _send_stream(used_cfg, is_fallback=True)
            except (httpx.TimeoutException, httpx.RequestError):
                _record_channel_failure(used_cfg, error_code=failure_reason)
                system_fallback_cfg = _system_fallback_config(
                    public_model,
                    used_cfg,
                    "responses",
                    messages_for_route,
                    tools_for_route,
                    lock_model_selection=resolved_model.lock_model_selection,
                    reason=failure_reason,
                )
                if system_fallback_cfg is None:
                    raise
                used_cfg = system_fallback_cfg
                used_route_reason = _system_fallback_route_reason(failure_reason)
                can_fallback = False
                is_cheap = False
                return await _send_stream(used_cfg, is_fallback=True)

        async def _provider_or_system_stream_fallback(failure_reason: str):
            route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
                public_model,
                used_cfg,
                "responses",
                messages_for_route,
                tools_for_route,
                lock_model_selection=resolved_model.lock_model_selection,
                reason=failure_reason,
            )
            if route_fallback_cfg is None:
                return None
            return await _retry_stream_with_cfg(route_fallback_cfg, route_fallback_reason, failure_reason)

        try:
            upstream = await _send_stream(used_cfg)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            if cpa_channel is not None:
                gemini_cpa.record_failure(cpa_channel)
            _record_channel_failure(used_cfg, error_code="upstream_unreachable")
            if _should_try_channel_fallback(used_cfg, error_code="upstream_unreachable"):
                try:
                    fallback_upstream = await _provider_or_system_stream_fallback("upstream_unreachable")
                except (httpx.TimeoutException, httpx.RequestError):
                    fallback_upstream = None
                if fallback_upstream is not None:
                    upstream = fallback_upstream
                else:
                    logger.error("upstream stream connect error: %s", exc)
                    return JSONResponse(
                        content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                        status_code=502,
                    )
            elif can_fallback:
                _fb = "cheap" if is_cheap else "premium"
                logger.warning("primary %s failed (%s: %s), falling back", _fb, type(exc).__name__, exc)
                used_cfg = _channel_fallback_config(used_cfg, fallback_cfg)
                used_route_reason = f"{_fb}_fallback_timeout"
                can_fallback = False
                is_cheap = False
                upstream = await _send_stream(used_cfg, is_fallback=True)
            else:
                logger.error("upstream stream connect error: %s", exc)
                return JSONResponse(
                    content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                    status_code=502,
                )

        if (
            upstream.status_code >= 400
            and _should_try_channel_fallback(used_cfg, status_code=upstream.status_code)
        ):
            _code = upstream.status_code
            route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
                public_model,
                used_cfg,
                "responses",
                messages_for_route,
                tools_for_route,
                lock_model_selection=resolved_model.lock_model_selection,
                reason=str(_code),
            )
            if route_fallback_cfg is not None:
                _err_body = await _close_stream_for_retry(upstream)
                logger.warning("provider channel stream %s returned %s: %s; falling back",
                               used_cfg.channel_id, _code, _err_body[:500])
                _record_channel_failure(used_cfg, status_code=_code)
                try:
                    upstream = await _retry_stream_with_cfg(route_fallback_cfg, route_fallback_reason, str(_code))
                except (httpx.TimeoutException, httpx.RequestError):
                    return JSONResponse(
                        content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                        status_code=502,
                    )

        if (
            upstream.status_code >= 400
            and _should_try_channel_fallback(used_cfg, status_code=upstream.status_code)
        ):
            _code = upstream.status_code
            system_fallback_cfg = _system_fallback_config(
                public_model,
                used_cfg,
                "responses",
                messages_for_route,
                tools_for_route,
                lock_model_selection=resolved_model.lock_model_selection,
                reason=str(_code),
            )
            if system_fallback_cfg is not None:
                _err_body = await _close_stream_for_retry(upstream)
                logger.warning("provider channel stream %s returned %s: %s; falling back to system",
                               used_cfg.channel_id, _code, _err_body[:500])
                _record_channel_failure(used_cfg, status_code=_code)
                try:
                    upstream = await _retry_stream_with_cfg(
                        system_fallback_cfg,
                        _system_fallback_route_reason(str(_code)),
                        str(_code),
                    )
                except (httpx.TimeoutException, httpx.RequestError):
                    return JSONResponse(
                        content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                        status_code=502,
                    )

        if (
            can_fallback
            and upstream.status_code >= 400
            and (
                not getattr(used_cfg, "channel_id", "")
                or should_record_channel_failure(upstream.status_code)
            )
        ):
            _fb = "cheap" if is_cheap else "premium"
            _code = upstream.status_code
            try:
                _err_body = await _close_stream_for_retry(upstream)
            except Exception:
                _err_body = b""
            logger.warning("primary %s returned %s: %s — falling back", _fb, _code, _err_body[:500])
            _record_channel_failure(used_cfg, status_code=_code)
            used_cfg = _channel_fallback_config(used_cfg, fallback_cfg)
            used_route_reason = f"{_fb}_fallback_{_code}"
            can_fallback = False
            is_cheap = False
            upstream = await _send_stream(used_cfg, is_fallback=True)

        if cpa_channel is not None:
            if upstream.status_code < 400:
                gemini_cpa.record_success(cpa_channel)
            elif gemini_cpa.should_record_failure(upstream.status_code):
                gemini_cpa.record_failure(cpa_channel)
        if upstream.status_code >= 400:
            _record_channel_failure(used_cfg, status_code=upstream.status_code)

        content_type = upstream.headers.get("content-type", "")
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        if "text/event-stream" not in content_type:
            route_fallback_cfg = None
            route_fallback_reason = ""
            if _should_try_channel_fallback(used_cfg, error_code="upstream_unexpected_content_type"):
                route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
                    public_model,
                    used_cfg,
                    "responses",
                    messages_for_route,
                    tools_for_route,
                    lock_model_selection=resolved_model.lock_model_selection,
                    reason="upstream_unexpected_content_type",
                )
            if route_fallback_cfg is not None:
                await _close_stream_for_retry(upstream)
                _record_channel_failure(used_cfg, error_code="upstream_unexpected_content_type")
                try:
                    upstream = await _retry_stream_with_cfg(
                        route_fallback_cfg,
                        route_fallback_reason,
                        "upstream_unexpected_content_type",
                    )
                except (httpx.TimeoutException, httpx.RequestError):
                    return JSONResponse(
                        content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                        status_code=502,
                    )
                content_type = upstream.headers.get("content-type", "")
                upstream_request_id = extract_upstream_request_id(upstream.headers)
            elif can_fallback:
                await _close_stream_for_retry(upstream)
                _fb = "cheap" if is_cheap else "premium"
                _record_channel_failure(used_cfg, error_code="upstream_unexpected_content_type")
                used_cfg = _channel_fallback_config(used_cfg, fallback_cfg)
                used_route_reason = f"{_fb}_fallback_unexpected"
                can_fallback = False
                is_cheap = False
                upstream = await _send_stream(used_cfg, is_fallback=True)
                content_type = upstream.headers.get("content-type", "")
                upstream_request_id = extract_upstream_request_id(upstream.headers)
            if "text/event-stream" not in content_type:
                try:
                    body = await upstream.aread()
                finally:
                    await upstream.aclose()
                response_headers = filter_headers(dict(upstream.headers))
                response_headers.pop("content-length", None)
                if upstream.status_code >= 400:
                    logger.error("upstream error %s: %s", upstream.status_code, body[:500])
                    _record_channel_failure(used_cfg, status_code=upstream.status_code)
                if "application/json" in content_type:
                    try:
                        data = json.loads(body.decode("utf-8"))
                        if isinstance(data, dict) and "model" in data:
                            data["model"] = display_model
                    except Exception:
                        data = {"detail": "upstream returned non-stream response"}
                    return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)
                return Response(content=body, status_code=upstream.status_code, headers=response_headers, media_type=content_type)

        stream_t0 = time.monotonic()
        _stream_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

        _model_mask = None
        if used_cfg.model_id != display_model:
            _model_mask = (used_cfg.model_id.encode(), display_model.encode())

        async def iter_bytes():
            buf = b""
            _resp_id_cap = None
            _resp_out_cap = None
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk.replace(_model_mask[0], _model_mask[1]) if _model_mask else chunk
                    buf += chunk
                    while b"\n\n" in buf:
                        event_raw, buf = buf.split(b"\n\n", 1)
                        for line in event_raw.split(b"\n"):
                            if line.startswith(b"data: "):
                                payload_str = line[6:].strip()
                                if payload_str == b"[DONE]":
                                    continue
                                try:
                                    evt = json.loads(payload_str)
                                    usage = evt.get("usage") or (evt.get("response") or {}).get("usage")
                                    if usage:
                                        _stream_usage["input"] = extract_total_input_tokens(usage)
                                        _stream_usage["output"] = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                                        _stream_usage["cache_read"] = extract_cache_read_tokens(usage)
                                        _stream_usage["cache_creation"] = extract_cache_creation_tokens(usage)
                                    _etype = evt.get("type", "")
                                    if _etype == "response.completed":
                                        _r = evt.get("response") or evt
                                        _resp_id_cap = _r.get("id")
                                        _resp_out_cap = _r.get("output", [])
                                    elif not _resp_id_cap and _etype == "response.created":
                                        _r = evt.get("response") or evt
                                        _rid = _r.get("id", "")
                                        if isinstance(_rid, str) and _rid:
                                            _resp_id_cap = _rid
                                except (json.JSONDecodeError, ValueError):
                                    pass
            finally:
                await upstream.aclose()
                if upstream.status_code < 400:
                    _record_channel_success(used_cfg, duration_ms=int((time.monotonic() - stream_t0) * 1000))
                    if _resp_id_cap:
                        _conv_cache.set(_resp_id_cap, _expanded_input, _resp_out_cap or [])
                        logger.info("polyfill: cached stream resp %s (%d in, %d out)",
                                    _resp_id_cap, len(_expanded_input), len(_resp_out_cap or []))
                    dur = int((time.monotonic() - stream_t0) * 1000)
                    asyncio.create_task(usage_buffer.add(
                        user.id,
                        api_key_id=api_key_id,
                        input_tokens=_stream_usage["input"],
                        output_tokens=_stream_usage["output"],
                        cache_read_tokens=_stream_usage["cache_read"],
                        cache_creation_tokens=_stream_usage["cache_creation"],
                        requests=1,
                        endpoint="responses:stream",
                        model=display_model,
                        customer_model_alias=display_model,
                        provider_model=public_model.provider_model or used_cfg.model_id,
                        route_reason=used_route_reason,
                        duration_ms=dur,
                        status_code=upstream.status_code,
                        price_input_per_million=price_input_per_million,
                        price_output_per_million=price_output_per_million,
                        usage_unit_type="tokens",
                        billable_sku=public_model.billable_sku or display_model,
                        upstream_request_id=upstream_request_id,
                        **_channel_usage_kwargs(used_cfg, cpa_channel),
                        **usage_pricing_kwargs(public_model, station_model),
                    ))
                else:
                    _record_channel_failure(used_cfg, status_code=upstream.status_code)

        stream_headers = filter_headers(dict(upstream.headers))
        stream_headers.pop("content-length", None)
        stream_headers.setdefault("cache-control", "no-cache")
        stream_headers.setdefault("x-accel-buffering", "no")
        return StreamingResponse(
            iter_bytes(),
            status_code=upstream.status_code,
            headers=stream_headers,
            media_type=upstream.headers.get("content-type"),
        )

    model_registry.ensure_initialized()
    fallback_cfg = model_registry.models.get("fallback") or model_registry.get("premium")
    cheap_cfg = model_registry.models.get("cheap")
    allow_fallback = public_model.routing_mode == "legacy_auto"
    is_cheap = bool(allow_fallback and cheap_cfg and used_cfg.model_id == cheap_cfg.model_id)
    can_fallback = allow_fallback and (
        (used_cfg.upstream_url != fallback_cfg.upstream_url) or (used_cfg.model_id != fallback_cfg.model_id)
    )
    if resolved_model.lock_model_selection and fallback_cfg.model_id != used_cfg.model_id:
        can_fallback = False
    client = await get_http_client()

    async def _post_json(cfg, *, is_fallback=False):
        nonlocal cpa_channel
        cpa_channel = _select_gemini_cpa_channel(cfg)
        send_payload = dict(base_payload)
        send_payload["model"] = cpa_channel.provider_model if cpa_channel is not None else cfg.model_id
        if is_fallback:
            send_payload.pop("previous_response_id", None)
        send_payload["store"] = True
        if "cognitiveservices.azure.com" in (cfg.upstream_url or ""):
            if "codex" not in (cfg.model_id or "").lower():
                send_payload.pop("reasoning", None)
        if cfg.strip_unsupported:
            for param in _STRIP_PARAMS:
                send_payload.pop(param, None)
        if cpa_channel is not None:
            send_payload = gemini_cpa.build_responses_chat_payload(send_payload, cpa_channel.provider_model)
            req_url = gemini_cpa.chat_completions_url(cpa_channel)
        else:
            req_url = _build_openai_responses_upstream_url(cfg.upstream_url)
        req_headers = gemini_cpa.build_headers(cpa_channel) if cpa_channel is not None else _build_upstream_headers(cfg)
        logger.info("json → %s  model=%s  store=%s  has_prev_resp=%s",
                    req_url, send_payload.get("model"), send_payload.get("store"),
                    "previous_response_id" in send_payload)
        t0 = time.monotonic()
        r = await client.post(req_url, json=send_payload, headers=req_headers)
        dur = int((time.monotonic() - t0) * 1000)
        return r, dur

    def _mark_system_fallback_terminal() -> None:
        nonlocal can_fallback, is_cheap
        if str(getattr(used_cfg, "channel_id", "") or "").startswith("system:"):
            can_fallback = False
            is_cheap = False

    try:
        upstream, duration_ms = await _post_json(used_cfg)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        if cpa_channel is not None:
            gemini_cpa.record_failure(cpa_channel)
        _record_channel_failure(used_cfg, error_code="upstream_unreachable")
        route_fallback_cfg = None
        route_fallback_reason = ""
        if _should_try_channel_fallback(used_cfg, error_code="upstream_unreachable"):
            route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
                public_model,
                used_cfg,
                "responses",
                messages_for_route,
                tools_for_route,
                lock_model_selection=resolved_model.lock_model_selection,
                reason="upstream_unreachable",
            )
        if route_fallback_cfg is not None:
            used_cfg = route_fallback_cfg
            used_route_reason = route_fallback_reason
            _mark_system_fallback_terminal()
            try:
                upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
            except (httpx.TimeoutException, httpx.RequestError):
                _record_channel_failure(used_cfg, error_code="upstream_unreachable")
                system_fallback_cfg = _system_fallback_config(
                    public_model,
                    used_cfg,
                    "responses",
                    messages_for_route,
                    tools_for_route,
                    lock_model_selection=resolved_model.lock_model_selection,
                    reason="upstream_unreachable",
                )
                if system_fallback_cfg is not None:
                    used_cfg = system_fallback_cfg
                    used_route_reason = _system_fallback_route_reason("upstream_unreachable")
                    _mark_system_fallback_terminal()
                    try:
                        upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
                    except (httpx.TimeoutException, httpx.RequestError):
                        _record_channel_failure(used_cfg, error_code="upstream_unreachable")
                        _notify_fallback_exhausted(
                            endpoint="responses",
                            model=display_model,
                            status_code=502,
                            reason="upstream_unreachable",
                            cfg=used_cfg,
                            route_reason=used_route_reason,
                        )
                        return JSONResponse(
                            content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                            status_code=502,
                        )
                else:
                    _notify_fallback_exhausted(
                        endpoint="responses",
                        model=display_model,
                        status_code=502,
                        reason="upstream_unreachable",
                        cfg=used_cfg,
                        route_reason=used_route_reason,
                    )
                    return JSONResponse(
                        content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                        status_code=502,
                    )
            except gemini_cpa.GeminiCpaChannelUnavailable as exc:
                return JSONResponse(
                    content={"error": {"message": str(exc), "type": "server_error", "code": "gemini_cpa_channel_cooling_down"}},
                    status_code=503,
                )
        elif can_fallback:
            _fb = "cheap" if is_cheap else "premium"
            logger.warning("primary %s failed (%s: %s), falling back", _fb, type(exc).__name__, exc)
            used_cfg = _channel_fallback_config(used_cfg, fallback_cfg)
            used_route_reason = f"{_fb}_fallback_timeout"
            can_fallback = False
            is_cheap = False
            upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
        else:
            logger.error("upstream request error: %s", exc)
            return JSONResponse(
                content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                status_code=502,
            )

    if (
        upstream.status_code >= 400
        and _should_try_channel_fallback(used_cfg, status_code=upstream.status_code)
    ):
        route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
            public_model,
            used_cfg,
            "responses",
            messages_for_route,
            tools_for_route,
            lock_model_selection=resolved_model.lock_model_selection,
            reason=str(upstream.status_code),
        )
        if route_fallback_cfg is not None:
            fallback_target = route_fallback_cfg.channel_id or route_fallback_cfg.provider_platform or "catalog"
            logger.warning("provider channel %s returned %s: %s; falling back to %s",
                           used_cfg.channel_id, upstream.status_code, str(upstream.text)[:500], fallback_target)
            _record_channel_failure(used_cfg, status_code=upstream.status_code)
            used_cfg = route_fallback_cfg
            used_route_reason = route_fallback_reason
            _mark_system_fallback_terminal()
            upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)

    if (
        upstream.status_code >= 400
        and _should_try_channel_fallback(used_cfg, status_code=upstream.status_code)
    ):
        system_fallback_cfg = _system_fallback_config(
            public_model,
            used_cfg,
            "responses",
            messages_for_route,
            tools_for_route,
            lock_model_selection=resolved_model.lock_model_selection,
            reason=str(upstream.status_code),
        )
        if system_fallback_cfg is not None:
            _record_channel_failure(used_cfg, status_code=upstream.status_code)
            used_cfg = system_fallback_cfg
            used_route_reason = _system_fallback_route_reason(str(upstream.status_code))
            _mark_system_fallback_terminal()
            upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)

    if (
        can_fallback
        and upstream.status_code >= 400
        and (
            not getattr(used_cfg, "channel_id", "")
            or should_record_channel_failure(upstream.status_code)
        )
    ):
        _fb = "cheap" if is_cheap else "premium"
        logger.warning("primary %s returned %s: %s — falling back",
                       _fb, upstream.status_code, str(upstream.text)[:500])
        _record_channel_failure(used_cfg, status_code=upstream.status_code)
        used_cfg = _channel_fallback_config(used_cfg, fallback_cfg)
        used_route_reason = f"{_fb}_fallback_{upstream.status_code}"
        can_fallback = False
        is_cheap = False
        upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
    if cpa_channel is not None:
        if upstream.status_code < 400:
            gemini_cpa.record_success(cpa_channel)
        elif gemini_cpa.should_record_failure(upstream.status_code):
            gemini_cpa.record_failure(cpa_channel)
    if upstream.status_code >= 400:
        _record_channel_failure(used_cfg, status_code=upstream.status_code)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    upstream_request_id = extract_upstream_request_id(upstream.headers)
    if (
        "application/json" not in content_type
        and _should_try_channel_fallback(used_cfg, error_code="upstream_unexpected_content_type")
    ):
        route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
            public_model,
            used_cfg,
            "responses",
            messages_for_route,
            tools_for_route,
            lock_model_selection=resolved_model.lock_model_selection,
            reason="upstream_unexpected_content_type",
        )
        if route_fallback_cfg is not None:
            _record_channel_failure(used_cfg, error_code="upstream_unexpected_content_type")
            used_cfg = route_fallback_cfg
            used_route_reason = route_fallback_reason
            _mark_system_fallback_terminal()
            upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
            response_headers = filter_headers(dict(upstream.headers))
            response_headers.pop("content-length", None)
            content_type = upstream.headers.get("content-type", "application/json")
            upstream_request_id = extract_upstream_request_id(upstream.headers)

    if can_fallback and "application/json" not in content_type:
        _fb = "cheap" if is_cheap else "premium"
        _record_channel_failure(used_cfg, error_code="upstream_unexpected_content_type")
        used_cfg = _channel_fallback_config(used_cfg, fallback_cfg)
        used_route_reason = f"{_fb}_fallback_unexpected"
        can_fallback = False
        is_cheap = False
        upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        content_type = upstream.headers.get("content-type", "application/json")
        upstream_request_id = extract_upstream_request_id(upstream.headers)

    if "application/json" in content_type:
        try:
            data = upstream.json()
        except Exception:
            route_fallback_cfg = None
            route_fallback_reason = ""
            if _should_try_channel_fallback(used_cfg, error_code="upstream_invalid_json"):
                route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
                    public_model,
                    used_cfg,
                    "responses",
                    messages_for_route,
                    tools_for_route,
                    lock_model_selection=resolved_model.lock_model_selection,
                    reason="upstream_invalid_json",
                )
            if route_fallback_cfg is not None:
                _record_channel_failure(used_cfg, error_code="upstream_invalid_json")
                used_cfg = route_fallback_cfg
                used_route_reason = route_fallback_reason
                _mark_system_fallback_terminal()
                upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
                response_headers = filter_headers(dict(upstream.headers))
                response_headers.pop("content-length", None)
                content_type = upstream.headers.get("content-type", "application/json")
                upstream_request_id = extract_upstream_request_id(upstream.headers)
                try:
                    data = upstream.json() if "application/json" in content_type else upstream.text
                except Exception:
                    _notify_fallback_exhausted(
                        endpoint="responses",
                        model=display_model,
                        status_code=502,
                        reason="upstream_invalid_json",
                        cfg=used_cfg,
                        route_reason=used_route_reason,
                        upstream_request_id=upstream_request_id,
                    )
                    return JSONResponse(
                        content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                        status_code=502, headers=response_headers,
                    )
            elif can_fallback:
                _fb = "cheap" if is_cheap else "premium"
                _record_channel_failure(used_cfg, error_code="upstream_invalid_json")
                used_cfg = _channel_fallback_config(used_cfg, fallback_cfg)
                used_route_reason = f"{_fb}_fallback_unexpected"
                can_fallback = False
                is_cheap = False
                upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
                response_headers = filter_headers(dict(upstream.headers))
                response_headers.pop("content-length", None)
                content_type = upstream.headers.get("content-type", "application/json")
                upstream_request_id = extract_upstream_request_id(upstream.headers)
                try:
                    data = upstream.json() if "application/json" in content_type else upstream.text
                except Exception:
                    _notify_fallback_exhausted(
                        endpoint="responses",
                        model=display_model,
                        status_code=502,
                        reason="upstream_invalid_json",
                        cfg=used_cfg,
                        route_reason=used_route_reason,
                        upstream_request_id=upstream_request_id,
                    )
                    return JSONResponse(
                        content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                        status_code=502, headers=response_headers,
                    )
            else:
                _record_channel_failure(used_cfg, error_code="upstream_invalid_json")
                _notify_fallback_exhausted(
                    endpoint="responses",
                    model=display_model,
                    status_code=502,
                    reason="upstream_invalid_json",
                    cfg=used_cfg,
                    route_reason=used_route_reason,
                    upstream_request_id=upstream_request_id,
                )
                return JSONResponse(
                    content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                    status_code=502, headers=response_headers,
                )
    else:
        data = upstream.text

    input_tokens_delta = 0
    output_tokens_delta = 0
    cache_read_tokens_delta = 0
    cache_creation_tokens_delta = 0
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        _notify_fallback_exhausted(
            endpoint="responses",
            model=display_model,
            status_code=upstream.status_code if upstream.status_code >= 400 else 502,
            reason=str((data.get("error") or {}).get("code") or upstream.status_code or "upstream_error"),
            cfg=used_cfg,
            route_reason=used_route_reason,
            upstream_request_id=upstream_request_id,
        )
        return JSONResponse(
            content={"error": data["error"]},
            status_code=upstream.status_code if upstream.status_code >= 400 else 502,
            headers=response_headers,
        )
    if upstream.status_code < 400 and isinstance(data, dict):
        if cpa_channel is not None:
            data = gemini_cpa.translate_chat_response_to_responses(data, display_model)
        if _responses_payload_is_empty_success(data):
            logger.error("responses upstream returned empty success payload for model=%s", display_model)
            if cpa_channel is not None:
                gemini_cpa.record_failure(cpa_channel)
            _record_channel_failure(used_cfg, error_code="upstream_empty_response")
            route_fallback_cfg = None
            route_fallback_reason = ""
            if _should_try_channel_fallback(used_cfg, error_code="upstream_empty_response"):
                route_fallback_cfg, route_fallback_reason = _next_provider_or_system_fallback_config(
                    public_model,
                    used_cfg,
                    "responses",
                    messages_for_route,
                    tools_for_route,
                    lock_model_selection=resolved_model.lock_model_selection,
                    reason="upstream_empty_response",
                )
            if route_fallback_cfg is not None:
                used_cfg = route_fallback_cfg
                used_route_reason = route_fallback_reason
                _mark_system_fallback_terminal()
                upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
                response_headers = filter_headers(dict(upstream.headers))
                response_headers.pop("content-length", None)
                content_type = upstream.headers.get("content-type", "application/json")
                upstream_request_id = extract_upstream_request_id(upstream.headers)
                try:
                    data = upstream.json() if "application/json" in content_type else upstream.text
                except Exception:
                    data = {"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}}
                if upstream.status_code < 400 and isinstance(data, dict) and cpa_channel is not None:
                    data = gemini_cpa.translate_chat_response_to_responses(data, display_model)
                if upstream.status_code < 400 and isinstance(data, dict) and not _responses_payload_is_empty_success(data):
                    usage = data.get("usage") or {}
                    input_tokens_delta = extract_total_input_tokens(usage)
                    output_tokens_delta = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                    cache_read_tokens_delta = extract_cache_read_tokens(usage)
                    cache_creation_tokens_delta = extract_cache_creation_tokens(usage)
                    _resp_id = data.get("id")
                    _resp_output = data.get("output")
                    if _resp_id and isinstance(_resp_output, list):
                        _conv_cache.set(_resp_id, _expanded_input, _resp_output)
                else:
                    _notify_fallback_exhausted(
                        endpoint="responses",
                        model=display_model,
                        status_code=502,
                        reason="upstream_empty_response",
                        cfg=used_cfg,
                        route_reason=used_route_reason,
                        upstream_request_id=upstream_request_id,
                    )
                    return JSONResponse(
                        content={
                            "error": {
                                "message": "Upstream completed without returning assistant text or tool calls",
                                "type": "server_error",
                                "code": "upstream_empty_response",
                            }
                        },
                        status_code=502,
                        headers=response_headers,
                    )
            else:
                _notify_fallback_exhausted(
                    endpoint="responses",
                    model=display_model,
                    status_code=502,
                    reason="upstream_empty_response",
                    cfg=used_cfg,
                    route_reason=used_route_reason,
                    upstream_request_id=upstream_request_id,
                )
                return JSONResponse(
                    content={
                        "error": {
                            "message": "Upstream completed without returning assistant text or tool calls",
                            "type": "server_error",
                            "code": "upstream_empty_response",
                        }
                    },
                    status_code=502,
                    headers=response_headers,
                )
        usage = data.get("usage") or {}
        input_tokens_delta = extract_total_input_tokens(usage)
        output_tokens_delta = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cache_read_tokens_delta = extract_cache_read_tokens(usage)
        cache_creation_tokens_delta = extract_cache_creation_tokens(usage)
        _resp_id = data.get("id")
        _resp_output = data.get("output")
        if _resp_id and isinstance(_resp_output, list):
            _conv_cache.set(_resp_id, _expanded_input, _resp_output)
            logger.info("polyfill: cached json resp %s (%d in, %d out)",
                        _resp_id, len(_expanded_input), len(_resp_output))

    if upstream.status_code < 400:
        _record_channel_success(used_cfg, duration_ms=duration_ms)
        await usage_buffer.add(
            user.id,
            api_key_id=api_key_id,
            input_tokens=input_tokens_delta,
            output_tokens=output_tokens_delta,
            cache_read_tokens=cache_read_tokens_delta,
            cache_creation_tokens=cache_creation_tokens_delta,
            requests=1,
            endpoint="responses",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            usage_unit_type="tokens",
            billable_sku=public_model.billable_sku or display_model,
            upstream_request_id=upstream_request_id,
            **_channel_usage_kwargs(used_cfg, cpa_channel),
            **usage_pricing_kwargs(public_model, station_model),
        )
    elif isinstance(data, (dict, str)):
        logger.error("upstream error %s: %s", upstream.status_code, str(data)[:500])

    if isinstance(data, dict):
        data["model"] = display_model
        if upstream.status_code >= 400:
            _notify_fallback_exhausted(
                endpoint="responses",
                model=display_model,
                status_code=upstream.status_code,
                reason=str((data.get("error") or {}).get("code") if isinstance(data.get("error"), dict) else upstream.status_code),
                cfg=used_cfg,
                route_reason=used_route_reason,
                upstream_request_id=upstream_request_id,
            )
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    if upstream.status_code >= 400:
        _notify_fallback_exhausted(
            endpoint="responses",
            model=display_model,
            status_code=upstream.status_code,
            reason=str(upstream.status_code or "upstream_error"),
            cfg=used_cfg,
            route_reason=used_route_reason,
            upstream_request_id=upstream_request_id,
        )
    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)


@router.post("/images/generations")
async def proxy_images_generations(request: Request, db: AsyncSession = Depends(get_db)):
    user = await authorize_request(request, db)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payload must be a json object")

    requested_model = str(payload.get("model") or "").strip()
    try:
        station_model = await resolve_station_model_for_user(db, user, requested_model, "images/generations")
        resolved_model = station_model.resolved_model if station_model else model_registry.resolve_public_model(requested_model, "images/generations")
    except Exception as exc:
        return _model_resolution_error_response(exc)

    public_model = resolved_model.public_model
    display_model = station_model.display_model if station_model else public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason
    price_per_image_cents = station_model.retail_price_per_image_cents if station_model else public_model.price_per_image_cents

    is_google_image_generation = public_model.provider_name.strip().lower() == "google"
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    should_use_gateway_image_generation = is_google_image_generation and delivery_lane == "gateway"
    should_use_cpa_gemini_image_generation = is_google_image_generation and delivery_lane == gemini_cpa.DELIVERY_LANE
    should_use_direct_vertex = is_google_image_generation and delivery_lane == "vertex_direct"
    client = None
    cpa_channel = None

    if (
        is_google_image_generation
        and not should_use_gateway_image_generation
        and not should_use_cpa_gemini_image_generation
        and not should_use_direct_vertex
    ):
        return _unsupported_google_image_lane_error(delivery_lane)

    if should_use_direct_vertex and not settings.vertex_api_key:
        return _openai_error_response(
            "Gemini image generation requires COINCOIN_VERTEX_API_KEY on the CoinCoin control plane.",
            error_type="server_error",
            code="vertex_image_generation_not_configured",
            status_code=503,
        )

    if is_google_image_generation and _requested_image_count_from_json(payload) > 1:
        return _vertex_image_candidate_count_error()

    if should_use_gateway_image_generation:
        stream_client = await get_image_stream_client()
        payload["model"] = used_cfg.model_id
        payload.pop("model_provider", None)
        upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/images/generations"
        headers = _build_upstream_headers(used_cfg)
        t0 = time.monotonic()
        upstream = await _send_stream_request(
            stream_client,
            "POST",
            upstream_url,
            json=payload,
            headers=headers,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
    elif should_use_cpa_gemini_image_generation:
        client = await get_http_client()
        try:
            cpa_channel = gemini_cpa.select_channel(public_model, used_cfg)
        except gemini_cpa.GeminiCpaChannelUnavailable as exc:
            return _openai_error_response(
                str(exc),
                error_type="server_error",
                code="gemini_cpa_channel_cooling_down",
                status_code=503,
            )
        upstream_payload = gemini_cpa.build_image_generation_payload(payload, cpa_channel.provider_model)
        upstream_url = gemini_cpa.chat_completions_url(cpa_channel)
        headers = gemini_cpa.build_headers(cpa_channel)
        t0 = time.monotonic()
        try:
            upstream = await _post_with_retries(client, upstream_url, json_body=upstream_payload, headers=headers)
        except httpx.TransportError as exc:
            gemini_cpa.record_failure(cpa_channel)
            _record_channel_failure(used_cfg, error_code="cpa_gemini_image_generation_transport_error")
            return _openai_error_response(
                f"Gemini CPA image generation transport error: {exc}",
                error_type="server_error",
                code="cpa_gemini_image_generation_transport_error",
                status_code=502,
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
    elif should_use_direct_vertex:
        client = await get_http_client()
        upstream_payload = _build_vertex_image_generation_payload(payload)
        upstream_url = (
            f"{settings.vertex_gemini_api_base.rstrip('/')}/models/"
            f"{public_model.provider_model or used_cfg.model_id}:generateContent"
        )
        headers = {
            "x-goog-api-key": settings.vertex_api_key,
            "content-type": "application/json",
        }
        t0 = time.monotonic()
        try:
            upstream = await _post_with_retries(client, upstream_url, json_body=upstream_payload, headers=headers)
        except httpx.TransportError as exc:
            return _openai_error_response(
                f"Vertex image generation transport error: {exc}",
                error_type="server_error",
                code="vertex_image_generation_transport_error",
                status_code=502,
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
    else:
        client = await get_http_client()
        payload["model"] = used_cfg.model_id
        payload.pop("model_provider", None)
        upstream_url = _build_openai_image_upstream_url(used_cfg.upstream_url, "images/generations")
        headers = _build_upstream_headers(used_cfg)
        t0 = time.monotonic()
        upstream = await client.post(
            upstream_url,
            json=payload,
            headers=headers,
            timeout=IMAGE_UPSTREAM_TIMEOUT,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)
    upstream_request_id = extract_upstream_request_id(upstream.headers)
    content_type = upstream.headers.get("content-type", "application/json")

    if should_use_gateway_image_generation and "application/json" in content_type:
        if upstream.status_code < 400:
            _record_channel_success(used_cfg, duration_ms=duration_ms)
            image_count = _requested_image_count_from_json(payload)
            await usage_buffer.add(
                user.id,
                api_key_id=getattr(user, _KEY_ID_ATTR, ""),
                requests=1,
                endpoint="images/generations",
                model=display_model,
                customer_model_alias=display_model,
                provider_model=public_model.provider_model or used_cfg.model_id,
                route_reason=used_route_reason,
                duration_ms=duration_ms,
                status_code=upstream.status_code,
                usage_unit_type="images",
                usage_unit_count=image_count,
                billable_sku=public_model.billable_sku or display_model,
                upstream_request_id=upstream_request_id,
                image_count=image_count,
                price_per_image_cents=price_per_image_cents,
                **_channel_usage_kwargs(used_cfg),
                **usage_pricing_kwargs(public_model, station_model),
            )
            return _stream_upstream_response(
                upstream,
                headers=response_headers,
                media_type=content_type,
            )
        try:
            upstream_body = await upstream.aread()
        finally:
            await upstream.aclose()
        try:
            data = json.loads(upstream_body.decode("utf-8"))
        except Exception:
            return JSONResponse(
                content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                status_code=502,
                headers=response_headers,
            )
        logger.error("image upstream error %s: %s", upstream.status_code, str(data)[:500])
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    if "application/json" in content_type:
        try:
            upstream_json = upstream.json()
        except Exception:
            if should_use_cpa_gemini_image_generation:
                gemini_cpa.record_failure(cpa_channel)
                _record_channel_failure(used_cfg, error_code="upstream_invalid_json")
            return JSONResponse(
                content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                status_code=502,
                headers=response_headers,
            )
        if should_use_cpa_gemini_image_generation and upstream.status_code < 400:
            data = gemini_cpa.translate_image_response(upstream_json if isinstance(upstream_json, dict) else {})
            if not data.get("data"):
                gemini_cpa.record_failure(cpa_channel)
                _record_channel_failure(used_cfg, error_code="upstream_empty_image_response")
                return _gemini_cpa_empty_image_error()
            gemini_cpa.record_success(cpa_channel)
            _record_channel_success(used_cfg, duration_ms=duration_ms)
        elif should_use_cpa_gemini_image_generation and upstream.status_code >= 400:
            data = upstream_json
            if gemini_cpa.should_record_failure(upstream.status_code):
                gemini_cpa.record_failure(cpa_channel)
            _record_channel_failure(used_cfg, status_code=upstream.status_code)
        elif should_use_direct_vertex and upstream.status_code < 400:
            data = _translate_vertex_image_response(upstream_json if isinstance(upstream_json, dict) else {})
        else:
            data = upstream_json
    else:
        data = upstream.text

    image_count = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        _record_channel_success(used_cfg, duration_ms=duration_ms)
        data_items = data.get("data")
        if isinstance(data_items, list) and data_items:
            image_count = len(data_items)
        else:
            try:
                image_count = max(1, int(payload.get("n") or 1))
            except (TypeError, ValueError):
                image_count = 1

        await usage_buffer.add(
            user.id,
            api_key_id=getattr(user, _KEY_ID_ATTR, ""),
            requests=1,
            endpoint="images/generations",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
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
            **usage_pricing_kwargs(public_model, station_model),
        )
    elif isinstance(data, (dict, str)):
        _record_channel_failure(used_cfg, status_code=upstream.status_code)
        logger.error("image upstream error %s: %s", upstream.status_code, str(data)[:500])

    if isinstance(data, dict):
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)


@router.post("/images/edits")
async def proxy_images_edits(request: Request, db: AsyncSession = Depends(get_db)):
    user = await authorize_request(request, db)

    try:
        requested_model, form_fields, file_fields = await _parse_image_edit_form(request)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid multipart payload") from exc

    try:
        station_model = await resolve_station_model_for_user(db, user, requested_model, "images/edits")
        resolved_model = station_model.resolved_model if station_model else model_registry.resolve_public_model(requested_model, "images/edits")
    except Exception as exc:
        return _model_resolution_error_response(exc)

    public_model = resolved_model.public_model
    display_model = station_model.display_model if station_model else public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason
    price_per_image_cents = station_model.retail_price_per_image_cents if station_model else public_model.price_per_image_cents

    response_headers: Dict[str, str] = {}

    is_google_image_edit = public_model.provider_name.strip().lower() == "google"
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    should_use_gateway_image_edit = is_google_image_edit and delivery_lane == "gateway"
    should_use_cpa_gemini_image_edit = is_google_image_edit and delivery_lane == gemini_cpa.DELIVERY_LANE
    should_use_direct_vertex = is_google_image_edit and delivery_lane == "vertex_direct"
    client = None
    cpa_channel = None
    input_image_count = sum(1 for key, _ in file_fields if key in {"image", "image[]"})
    total_upload_bytes = sum(len(content) for key, (_, content, _) in file_fields if key in {"image", "image[]"})

    if (
        is_google_image_edit
        and not should_use_gateway_image_edit
        and not should_use_cpa_gemini_image_edit
        and not should_use_direct_vertex
    ):
        return _unsupported_google_image_lane_error(delivery_lane)

    if should_use_direct_vertex and not settings.vertex_api_key:
        return _openai_error_response(
            "Gemini image edits require COINCOIN_VERTEX_API_KEY on the CoinCoin control plane.",
            error_type="server_error",
            code="vertex_image_edit_not_configured",
            status_code=503,
        )

    if is_google_image_edit and _requested_image_count_from_pairs(form_fields) > 1:
        return _vertex_image_candidate_count_error()

    if is_google_image_edit and input_image_count > max(1, int(settings.image_job_sync_input_limit or 2)):
        if not settings.image_jobs_enabled:
            return _openai_error_response(
                "This deployment requires async image jobs for multi-image Gemini edits, but image jobs are disabled.",
                code="image_jobs_disabled",
                error_type="server_error",
                status_code=503,
            )
        if input_image_count > max(1, int(settings.image_job_async_max_inputs or 8)):
            return _openai_error_response(
                f"Async image jobs currently support up to {settings.image_job_async_max_inputs} input images.",
                code="image_job_input_limit_exceeded",
                param="image",
                status_code=400,
            )
        return _image_job_required_error(input_image_count)

    if should_use_gateway_image_edit:
        stream_client = await get_image_stream_client()
        if any(key in {"mask", "mask[]"} for key, _ in file_fields):
            return _openai_error_response(
                "Gemini image edits via the current gateway lane do not support mask uploads.",
                code="mask_not_supported",
                param="mask",
                status_code=400,
            )

        upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/images/edits"
        headers = _build_upstream_headers(used_cfg)

        upstream_form_fields = [(key, value) for key, value in form_fields if key != "model"]
        upstream_form_fields.append(("model", used_cfg.model_id))
        multipart_body, multipart_content_type = _encode_multipart_form_data(upstream_form_fields, file_fields)
        headers["content-type"] = multipart_content_type

        t0 = time.monotonic()
        try:
            upstream = await asyncio.wait_for(
                _send_stream_request(
                    stream_client,
                    "POST",
                    upstream_url,
                    content=multipart_body,
                    headers=headers,
                ),
                timeout=max(1, int(settings.image_edit_sync_gateway_timeout_seconds or 60)),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "gateway image edit exceeded sync budget; forcing async fallback image_count=%s upload_bytes=%s model=%s",
                input_image_count,
                total_upload_bytes,
                display_model,
            )
            _record_channel_failure(used_cfg, error_code="upstream_timeout")
            if settings.image_jobs_enabled and input_image_count <= max(1, int(settings.image_job_async_max_inputs or 8)):
                return _image_job_timeout_error(input_image_count)
            return _openai_error_response(
                "Gemini image edit exceeded the sync response budget and async image jobs are unavailable for this request.",
                code="upstream_timeout",
                status_code=504,
                error_type="server_error",
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("gateway image edit transport error: %s", exc)
            _record_channel_failure(used_cfg, error_code="upstream_unreachable")
            return _openai_error_response(
                f"Gateway image edit request failed: {exc}",
                code="upstream_unreachable",
                status_code=502,
                error_type="server_error",
            )
        duration_ms = int((time.monotonic() - t0) * 1000)

        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        content_type = upstream.headers.get("content-type", "application/json")

        if "application/json" in content_type and upstream.status_code < 400:
            _record_channel_success(used_cfg, duration_ms=duration_ms)
            image_count = _requested_image_count_from_pairs(form_fields)
            await usage_buffer.add(
                user.id,
                api_key_id=getattr(user, _KEY_ID_ATTR, ""),
                requests=1,
                endpoint="images/edits",
                model=display_model,
                customer_model_alias=display_model,
                provider_model=public_model.provider_model or used_cfg.model_id,
                route_reason=used_route_reason,
                duration_ms=duration_ms,
                status_code=upstream.status_code,
                usage_unit_type="images",
                usage_unit_count=image_count,
                billable_sku=public_model.billable_sku or display_model,
                upstream_request_id=upstream_request_id,
                image_count=image_count,
                price_per_image_cents=price_per_image_cents,
                **_channel_usage_kwargs(used_cfg),
                **usage_pricing_kwargs(public_model, station_model),
            )
            return _stream_upstream_response(
                upstream,
                headers=response_headers,
                media_type=content_type,
            )

        try:
            upstream_body = await upstream.aread()
        finally:
            await upstream.aclose()
        if "application/json" in content_type:
            try:
                data = json.loads(upstream_body.decode("utf-8"))
            except Exception:
                return JSONResponse(
                    content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                    status_code=502,
                    headers=response_headers,
                )
        else:
            data = upstream_body.decode("utf-8", errors="replace")
    elif should_use_cpa_gemini_image_edit:
        client = await get_http_client()
        if any(key in {"mask", "mask[]"} for key, _ in file_fields):
            return _openai_error_response(
                "Gemini image edits via the Gemini CPA lane do not support mask uploads.",
                code="mask_not_supported",
                param="mask",
                status_code=400,
            )

        try:
            cpa_channel = gemini_cpa.select_channel(public_model, used_cfg)
        except gemini_cpa.GeminiCpaChannelUnavailable as exc:
            return _openai_error_response(
                str(exc),
                error_type="server_error",
                code="gemini_cpa_channel_cooling_down",
                status_code=503,
            )
        try:
            payload = gemini_cpa.build_image_edit_payload(form_fields, file_fields, cpa_channel.provider_model)
        except ValueError as exc:
            return _openai_error_response(str(exc), code="invalid_image_request", status_code=400)
        upstream_url = gemini_cpa.chat_completions_url(cpa_channel)
        headers = gemini_cpa.build_headers(cpa_channel)
        t0 = time.monotonic()
        try:
            upstream = await _post_with_retries(client, upstream_url, json_body=payload, headers=headers)
        except httpx.TransportError as exc:
            gemini_cpa.record_failure(cpa_channel)
            _record_channel_failure(used_cfg, error_code="cpa_gemini_image_edit_transport_error")
            return _openai_error_response(
                f"Gemini CPA image edit transport error: {exc}",
                error_type="server_error",
                code="cpa_gemini_image_edit_transport_error",
                status_code=502,
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        try:
            upstream_data = upstream.json()
        except Exception:
            gemini_cpa.record_failure(cpa_channel)
            _record_channel_failure(used_cfg, error_code="upstream_invalid_json")
            return JSONResponse(
                content={"error": {"message": "Gemini CPA returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                status_code=502,
                headers=response_headers,
            )

        if upstream.status_code < 400:
            data = gemini_cpa.translate_image_response(upstream_data if isinstance(upstream_data, dict) else {})
            if not data.get("data"):
                gemini_cpa.record_failure(cpa_channel)
                _record_channel_failure(used_cfg, error_code="upstream_empty_image_response")
                return _gemini_cpa_empty_image_error()
            gemini_cpa.record_success(cpa_channel)
            _record_channel_success(used_cfg, duration_ms=duration_ms)
        else:
            data = upstream_data
            if gemini_cpa.should_record_failure(upstream.status_code):
                gemini_cpa.record_failure(cpa_channel)
            _record_channel_failure(used_cfg, status_code=upstream.status_code)
    elif should_use_direct_vertex:
        client = await get_http_client()
        if any(key in {"mask", "mask[]"} for key, _ in file_fields):
            return _openai_error_response(
                "Gemini image edits via the current Vertex API-key lane do not support mask uploads.",
                code="mask_not_supported",
                param="mask",
                status_code=400,
            )

        payload = _build_vertex_image_edit_payload(form_fields, file_fields)
        upstream_url = (
            f"{settings.vertex_gemini_api_base.rstrip('/')}/models/"
            f"{public_model.provider_model or used_cfg.model_id}:generateContent"
        )
        headers = {
            "x-goog-api-key": settings.vertex_api_key,
            "content-type": "application/json",
        }
        t0 = time.monotonic()
        try:
            upstream = await _post_with_retries(client, upstream_url, json_body=payload, headers=headers)
        except httpx.TransportError as exc:
            return _openai_error_response(
                f"Vertex image edit transport error: {exc}",
                error_type="server_error",
                code="vertex_image_edit_transport_error",
                status_code=502,
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        try:
            upstream_data = upstream.json()
        except Exception:
            return JSONResponse(
                content={"error": {"message": "Vertex returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                status_code=502,
                headers=response_headers,
            )

        if upstream.status_code < 400:
            data = _translate_vertex_image_response(upstream_data if isinstance(upstream_data, dict) else {})
        else:
            error_message = str(upstream_data)
            if isinstance(upstream_data, dict):
                err = upstream_data.get("error")
                if isinstance(err, dict):
                    error_message = str(err.get("message") or err)
            return _openai_error_response(
                error_message,
                error_type="server_error",
                code="vertex_image_edit_failed",
                status_code=upstream.status_code,
            )
    else:
        stream_client = await get_image_stream_client()
        upstream_url = _build_openai_image_upstream_url(used_cfg.upstream_url, "images/edits")
        headers = _build_upstream_headers(used_cfg)

        upstream_form_fields = [(key, value) for key, value in form_fields if key != "model"]
        upstream_form_fields.append(("model", used_cfg.model_id))
        multipart_body, multipart_content_type = _encode_multipart_form_data(upstream_form_fields, file_fields)
        headers["content-type"] = multipart_content_type

        t0 = time.monotonic()
        try:
            upstream = await _send_stream_request(
                stream_client,
                "POST",
                upstream_url,
                content=multipart_body,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("OpenAI image edit transport error: %s", exc)
            _record_channel_failure(used_cfg, error_code="upstream_unreachable")
            return _openai_error_response(
                f"OpenAI image edit request failed: {exc}",
                code="upstream_unreachable",
                status_code=502,
                error_type="server_error",
            )
        duration_ms = int((time.monotonic() - t0) * 1000)

        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        content_type = upstream.headers.get("content-type", "application/json")
        upstream_request_id = extract_upstream_request_id(upstream.headers)

        if "application/json" in content_type and upstream.status_code < 400:
            _record_channel_success(used_cfg, duration_ms=duration_ms)
            image_count = _requested_image_count_from_pairs(form_fields)
            await usage_buffer.add(
                user.id,
                api_key_id=getattr(user, _KEY_ID_ATTR, ""),
                requests=1,
                endpoint="images/edits",
                model=display_model,
                customer_model_alias=display_model,
                provider_model=public_model.provider_model or used_cfg.model_id,
                route_reason=used_route_reason,
                duration_ms=duration_ms,
                status_code=upstream.status_code,
                usage_unit_type="images",
                usage_unit_count=image_count,
                billable_sku=public_model.billable_sku or display_model,
                upstream_request_id=upstream_request_id,
                image_count=image_count,
                price_per_image_cents=price_per_image_cents,
                **_channel_usage_kwargs(used_cfg),
                **usage_pricing_kwargs(public_model, station_model),
            )
            return _stream_upstream_response(
                upstream,
                headers=response_headers,
                media_type=content_type,
            )

        try:
            upstream_body = await upstream.aread()
        finally:
            await upstream.aclose()
        if "application/json" in content_type:
            try:
                data = json.loads(upstream_body.decode("utf-8"))
            except Exception:
                return JSONResponse(
                    content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                    status_code=502,
                    headers=response_headers,
                )
        else:
            data = upstream_body.decode("utf-8", errors="replace")

    image_count = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        _record_channel_success(used_cfg, duration_ms=duration_ms)
        data_items = data.get("data")
        if isinstance(data_items, list) and data_items:
            image_count = len(data_items)
        else:
            image_count = _requested_image_count_from_pairs(form_fields)

        await usage_buffer.add(
            user.id,
            api_key_id=getattr(user, _KEY_ID_ATTR, ""),
            requests=1,
            endpoint="images/edits",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
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
            **usage_pricing_kwargs(public_model, station_model),
        )
    elif isinstance(data, (dict, str)):
        _record_channel_failure(used_cfg, status_code=upstream.status_code)
        logger.error("image edit upstream error %s: %s", upstream.status_code, str(data)[:500])

    if isinstance(data, dict):
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)
