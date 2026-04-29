import asyncio
import base64
import json
import logging
import secrets
import time
from urllib.parse import urlsplit, urlunsplit
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import settings
from .db import get_db
from .models import ApiKey, UsageDaily, User
from .rate_limiter import rate_limiter
from .security import extract_api_key, hash_key
from .router import (
    ModelCapabilityError,
    UnknownModelError,
    extract_messages_for_routing_from_responses_payload,
    registry as model_registry,
)
from .usage_buffer import extract_cached_tokens, usage_buffer

_KEY_KIND_ATTR = "_key_kind"
_ENCRYPTED_PREFIXES = ("gAAA", "gBAA")
_ID_STRIP_PREFIXES = ("resp_", "msg_", "fc_", "fco_", "rs_")
_CONTENT_KEYS = frozenset({
    "text", "content", "output", "arguments", "instructions",
    "description", "name", "url", "title",
})


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


def _build_openai_image_upstream_url(base_url: str, endpoint: str) -> str:
    normalized = _normalize_openai_image_base_url(base_url)
    return f"{normalized}/{endpoint.lstrip('/')}"


def _responses_text_input_item(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text or ""}],
    }


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


key_cache = KeyCache(settings.key_cache_ttl, settings.key_cache_max)


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

        if api_key.expires_at and api_key.expires_at < datetime.utcnow():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired, please login again")

        user = api_key.user
        key_kind = getattr(api_key, "kind", None) or "api"
        await key_cache.set(
            key_hash,
            {
                "id": user.id,
                _KEY_KIND_ATTR: key_kind,
            },
        )
    setattr(user, _KEY_KIND_ATTR, key_kind)
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user blocked")
    return user


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
        current_balance = getattr(user, "balance", 0) or 0
        if current_balance - pending_cost <= 0:
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient balance")

    if user.request_limit_per_day is not None:
        today = date.today()
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
        resolved_model = model_registry.resolve_public_model(requested_model, "responses", messages_for_route, tools_for_route)
    except Exception as exc:
        return _model_resolution_error_response(exc)
    public_model = resolved_model.public_model
    display_model = public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason

    payload["model"] = used_cfg.model_id
    payload.pop("model_provider", None)
    _sanitize_encrypted_ids(payload)
    _ensure_content_text(payload)

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

    upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/responses"

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
            send_payload = dict(base_payload)
            send_payload["model"] = cfg.model_id
            if is_fallback:
                send_payload.pop("previous_response_id", None)
            send_payload["store"] = True
            if "cognitiveservices.azure.com" in (cfg.upstream_url or ""):
                if "codex" not in (cfg.model_id or "").lower():
                    send_payload.pop("reasoning", None)
            if cfg.strip_unsupported:
                for param in _STRIP_PARAMS:
                    send_payload.pop(param, None)
            req_url = f"{cfg.upstream_url.rstrip('/')}/responses"
            req_headers = _build_upstream_headers(cfg)
            logger.info("stream → %s  model=%s  store=%s  has_prev_resp=%s  input_types=%s",
                        req_url, send_payload.get("model"), send_payload.get("store"),
                        "previous_response_id" in send_payload,
                        [i.get("type") for i in send_payload.get("input", []) if isinstance(i, dict)])
            req = stream_client.build_request("POST", req_url, json=send_payload, headers=req_headers)
            return await stream_client.send(req, stream=True)

        try:
            upstream = await _send_stream(used_cfg)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            if can_fallback:
                _fb = "cheap" if is_cheap else "premium"
                logger.warning("primary %s failed (%s: %s), falling back", _fb, type(exc).__name__, exc)
                used_cfg = fallback_cfg
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

        if can_fallback and upstream.status_code >= 400:
            _fb = "cheap" if is_cheap else "premium"
            _code = upstream.status_code
            _err_body = b""
            try:
                _err_body = await upstream.aread()
            except Exception:
                pass
            try:
                await upstream.aclose()
            except Exception:
                pass
            logger.warning("primary %s returned %s: %s — falling back", _fb, _code, _err_body[:500])
            used_cfg = fallback_cfg
            used_route_reason = f"{_fb}_fallback_{_code}"
            can_fallback = False
            is_cheap = False
            upstream = await _send_stream(used_cfg, is_fallback=True)

        content_type = upstream.headers.get("content-type", "")
        upstream_request_id = extract_upstream_request_id(upstream.headers)
        if "text/event-stream" not in content_type:
            if can_fallback:
                try:
                    await upstream.aclose()
                except Exception:
                    pass
                _fb = "cheap" if is_cheap else "premium"
                used_cfg = fallback_cfg
                used_route_reason = f"{_fb}_fallback_unexpected"
                can_fallback = False
                is_cheap = False
                upstream = await _send_stream(used_cfg, is_fallback=True)
                content_type = upstream.headers.get("content-type", "")
                upstream_request_id = extract_upstream_request_id(upstream.headers)
            try:
                body = await upstream.aread()
            finally:
                await upstream.aclose()
            response_headers = filter_headers(dict(upstream.headers))
            response_headers.pop("content-length", None)
            if upstream.status_code >= 400:
                logger.error("upstream error %s: %s", upstream.status_code, body[:500])
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
        _stream_usage = {"input": 0, "output": 0, "cached": 0}

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
                                        _stream_usage["input"] = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                                        _stream_usage["output"] = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                                        _stream_usage["cached"] = extract_cached_tokens(usage)
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
                    if _resp_id_cap:
                        _conv_cache.set(_resp_id_cap, _expanded_input, _resp_out_cap or [])
                        logger.info("polyfill: cached stream resp %s (%d in, %d out)",
                                    _resp_id_cap, len(_expanded_input), len(_resp_out_cap or []))
                    dur = int((time.monotonic() - stream_t0) * 1000)
                    asyncio.create_task(usage_buffer.add(
                        user.id,
                        input_tokens=_stream_usage["input"],
                        output_tokens=_stream_usage["output"],
                        cached_tokens=_stream_usage["cached"],
                        requests=1,
                        endpoint="responses:stream",
                        model=display_model,
                        customer_model_alias=display_model,
                        provider_model=public_model.provider_model or used_cfg.model_id,
                        route_reason=used_route_reason,
                        duration_ms=dur,
                        status_code=upstream.status_code,
                        price_input_per_million=used_cfg.price_input_per_million,
                        price_output_per_million=used_cfg.price_output_per_million,
                        usage_unit_type="tokens",
                        billable_sku=public_model.billable_sku or display_model,
                        upstream_request_id=upstream_request_id,
                    ))

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
        send_payload = dict(base_payload)
        send_payload["model"] = cfg.model_id
        if is_fallback:
            send_payload.pop("previous_response_id", None)
        send_payload["store"] = True
        if "cognitiveservices.azure.com" in (cfg.upstream_url or ""):
            if "codex" not in (cfg.model_id or "").lower():
                send_payload.pop("reasoning", None)
        if cfg.strip_unsupported:
            for param in _STRIP_PARAMS:
                send_payload.pop(param, None)
        req_url = f"{cfg.upstream_url.rstrip('/')}/responses"
        req_headers = _build_upstream_headers(cfg)
        logger.info("json → %s  model=%s  store=%s  has_prev_resp=%s",
                    req_url, send_payload.get("model"), send_payload.get("store"),
                    "previous_response_id" in send_payload)
        t0 = time.monotonic()
        r = await client.post(req_url, json=send_payload, headers=req_headers)
        dur = int((time.monotonic() - t0) * 1000)
        return r, dur

    try:
        upstream, duration_ms = await _post_json(used_cfg)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        if can_fallback:
            _fb = "cheap" if is_cheap else "premium"
            logger.warning("primary %s failed (%s: %s), falling back", _fb, type(exc).__name__, exc)
            used_cfg = fallback_cfg
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

    if can_fallback and upstream.status_code >= 400:
        _fb = "cheap" if is_cheap else "premium"
        logger.warning("primary %s returned %s: %s — falling back",
                       _fb, upstream.status_code, str(upstream.text)[:500])
        used_cfg = fallback_cfg
        used_route_reason = f"{_fb}_fallback_{upstream.status_code}"
        can_fallback = False
        is_cheap = False
        upstream, duration_ms = await _post_json(used_cfg, is_fallback=True)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    upstream_request_id = extract_upstream_request_id(upstream.headers)
    if can_fallback and "application/json" not in content_type:
        _fb = "cheap" if is_cheap else "premium"
        used_cfg = fallback_cfg
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
            if can_fallback:
                _fb = "cheap" if is_cheap else "premium"
                used_cfg = fallback_cfg
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
                    return JSONResponse(
                        content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                        status_code=502, headers=response_headers,
                    )
            else:
                return JSONResponse(
                    content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                    status_code=502, headers=response_headers,
                )
    else:
        data = upstream.text

    input_tokens_delta = 0
    output_tokens_delta = 0
    cached_tokens_delta = 0
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return JSONResponse(
            content={"error": data["error"]},
            status_code=upstream.status_code if upstream.status_code >= 400 else 502,
            headers=response_headers,
        )
    if upstream.status_code < 400 and isinstance(data, dict):
        if _responses_payload_is_empty_success(data):
            logger.error("responses upstream returned empty success payload for model=%s", display_model)
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
        input_tokens_delta = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens_delta = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cached_tokens_delta = extract_cached_tokens(usage)
        _resp_id = data.get("id")
        _resp_output = data.get("output")
        if _resp_id and isinstance(_resp_output, list):
            _conv_cache.set(_resp_id, _expanded_input, _resp_output)
            logger.info("polyfill: cached json resp %s (%d in, %d out)",
                        _resp_id, len(_expanded_input), len(_resp_output))

    if upstream.status_code < 400:
        await usage_buffer.add(
            user.id,
            input_tokens=input_tokens_delta,
            output_tokens=output_tokens_delta,
            cached_tokens=cached_tokens_delta,
            requests=1,
            endpoint="responses",
            model=display_model,
            customer_model_alias=display_model,
            provider_model=public_model.provider_model or used_cfg.model_id,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=used_cfg.price_input_per_million,
            price_output_per_million=used_cfg.price_output_per_million,
            usage_unit_type="tokens",
            billable_sku=public_model.billable_sku or display_model,
            upstream_request_id=upstream_request_id,
        )
    elif isinstance(data, (dict, str)):
        logger.error("upstream error %s: %s", upstream.status_code, str(data)[:500])

    if isinstance(data, dict):
        data["model"] = display_model
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

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
        resolved_model = model_registry.resolve_public_model(requested_model, "images/generations")
    except Exception as exc:
        return _model_resolution_error_response(exc)

    public_model = resolved_model.public_model
    display_model = public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason

    is_google_image_generation = public_model.provider_name.strip().lower() == "google"
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    should_use_gateway_image_generation = is_google_image_generation and delivery_lane == "gateway"
    should_use_direct_vertex = is_google_image_generation and delivery_lane == "vertex_direct"
    client = None

    if is_google_image_generation and not should_use_gateway_image_generation and not should_use_direct_vertex:
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
            image_count = _requested_image_count_from_json(payload)
            await usage_buffer.add(
                user.id,
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
                price_per_image_cents=public_model.price_per_image_cents,
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
            return JSONResponse(
                content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                status_code=502,
                headers=response_headers,
            )
        if is_google_image_generation and not should_use_gateway_image_generation and upstream.status_code < 400:
            data = _translate_vertex_image_response(upstream_json if isinstance(upstream_json, dict) else {})
        else:
            data = upstream_json
    else:
        data = upstream.text

    image_count = 0
    if upstream.status_code < 400 and isinstance(data, dict):
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
            price_per_image_cents=public_model.price_per_image_cents,
        )
    elif isinstance(data, (dict, str)):
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
        resolved_model = model_registry.resolve_public_model(requested_model, "images/edits")
    except Exception as exc:
        return _model_resolution_error_response(exc)

    public_model = resolved_model.public_model
    display_model = public_model.public_id
    used_cfg = resolved_model.backend
    used_route_reason = resolved_model.route_reason

    response_headers: Dict[str, str] = {}

    is_google_image_edit = public_model.provider_name.strip().lower() == "google"
    delivery_lane = (public_model.delivery_lane or "").strip().lower()
    should_use_gateway_image_edit = is_google_image_edit and delivery_lane == "gateway"
    should_use_direct_vertex = is_google_image_edit and delivery_lane == "vertex_direct"
    client = None
    input_image_count = sum(1 for key, _ in file_fields if key in {"image", "image[]"})
    total_upload_bytes = sum(len(content) for key, (_, content, _) in file_fields if key in {"image", "image[]"})

    if is_google_image_edit and not should_use_gateway_image_edit and not should_use_direct_vertex:
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
            image_count = _requested_image_count_from_pairs(form_fields)
            await usage_buffer.add(
                user.id,
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
                price_per_image_cents=public_model.price_per_image_cents,
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
        client = await get_http_client()
        upstream_url = _build_openai_image_upstream_url(used_cfg.upstream_url, "images/edits")
        headers = _build_upstream_headers(used_cfg)
        headers.pop("content-type", None)

        upstream_form_fields = [(key, value) for key, value in form_fields if key != "model"]
        upstream_form_fields.append(("model", used_cfg.model_id))

        t0 = time.monotonic()
        upstream = await client.post(
            upstream_url,
            data=upstream_form_fields,
            files=file_fields,
            headers=headers,
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
                return JSONResponse(
                    content={"error": {"message": "Upstream returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                    status_code=502,
                    headers=response_headers,
                )
        else:
            data = upstream.text

    image_count = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        data_items = data.get("data")
        if isinstance(data_items, list) and data_items:
            image_count = len(data_items)
        else:
            image_count = _requested_image_count_from_pairs(form_fields)

        await usage_buffer.add(
            user.id,
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
            price_per_image_cents=public_model.price_per_image_cents,
        )
    elif isinstance(data, (dict, str)):
        logger.error("image edit upstream error %s: %s", upstream.status_code, str(data)[:500])

    if isinstance(data, dict):
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)
