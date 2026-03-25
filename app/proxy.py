import asyncio
import base64
import json
import logging
import time
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import settings
from .db import get_db
from .models import ApiKey, UsageDaily
from .rate_limiter import rate_limiter
from .security import extract_api_key, hash_key
from .router import (
    ModelCapabilityError,
    UnknownModelError,
    build_model_cloak,
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


def _expand_previous_response_input(payload: dict, cached_conv: Optional[Tuple[list, list]]) -> Optional[Tuple[int, int, int]]:
    if not cached_conv:
        return None

    prev_input, prev_output = cached_conv
    prev_input_items = _normalize_responses_input_items(prev_input)
    prev_output_items = _clone_responses_items(prev_output)
    cur_input_items = _normalize_responses_input_items(payload.get("input"))

    payload["input"] = prev_input_items + prev_output_items + cur_input_items
    return len(prev_input_items), len(prev_output_items), len(cur_input_items)


router = APIRouter(prefix="/openai/v1", tags=["proxy"])
logger = logging.getLogger("coincoin.proxy")

_http_client: Optional[httpx.AsyncClient] = None
_http_stream_client: Optional[httpx.AsyncClient] = None
_http_lock = asyncio.Lock()


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
        stream_timeout = httpx.Timeout(connect=5.0, read=120.0, write=60.0, pool=60.0)
        _http_stream_client = httpx.AsyncClient(limits=limits, timeout=stream_timeout, trust_env=False)
        return _http_stream_client


async def close_http_client() -> None:
    global _http_client, _http_stream_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
    if _http_stream_client and not _http_stream_client.is_closed:
        await _http_stream_client.aclose()
    _http_stream_client = None


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

    _TTL = 1800  # 30 min
    _MAX = 10000

    def __init__(self) -> None:
        self._data: Dict[str, Tuple[float, list, list]] = {}

    def get(self, response_id: str) -> Optional[Tuple[list, list]]:
        item = self._data.get(response_id)
        if not item:
            return None
        expires_at, expanded_input, response_output = item
        if expires_at <= time.time():
            self._data.pop(response_id, None)
            return None
        return expanded_input, response_output

    def set(self, response_id: str, expanded_input: list, response_output: list) -> None:
        now = time.time()
        if len(self._data) >= self._MAX:
            cutoff = now
            stale = [k for k, (exp, _, _) in self._data.items() if exp <= cutoff]
            for k in stale:
                self._data.pop(k, None)
            if len(self._data) >= self._MAX:
                self._data.pop(next(iter(self._data)), None)
        self._data[response_id] = (now + self._TTL, expanded_input, response_output)


_conv_cache = ResponseConversationCache()


HOP_BY_HOP_HEADERS = {
    "connection",
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


def _map_image_size_to_aspect_ratio(size: str) -> str:
    aspect_ratio_map = {
        "1024x1024": "1:1",
        "1792x1024": "16:9",
        "1024x1792": "9:16",
        "1280x896": "4:3",
        "896x1280": "3:4",
    }
    return aspect_ratio_map.get(size, "1:1")


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
        "contents": [{"parts": parts}],
        "generationConfig": generation_config,
    }


def _translate_vertex_image_edit_response(data: Dict[str, object]) -> Dict[str, object]:
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
        user = SimpleNamespace(**cached)
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
                "status": user.status,
                "balance": user.balance,
                "token_limit": user.token_limit,
                "token_used": user.token_used,
                "request_limit_per_minute": user.request_limit_per_minute,
                "request_limit_per_day": user.request_limit_per_day,
                _KEY_KIND_ATTR: key_kind,
            },
        )
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

    _has_tools = bool(payload.get("tools"))
    if settings.model_cloak and display_model and not _has_tools:
        _cloak = build_model_cloak(display_model, public_model)
        payload["instructions"] = (payload.get("instructions") or "") + _cloak

    _text = payload.get("text")
    if isinstance(_text, dict) and "verbosity" in _text:
        _text["verbosity"] = "medium"

    _prev_resp_id = payload.get("previous_response_id")
    if _prev_resp_id:
        _cached_conv = _conv_cache.get(_prev_resp_id)
        if _cached_conv:
            _expanded_counts = _expand_previous_response_input(payload, _cached_conv)
            logger.info("polyfill: expanded from %s (%d+%d+%d items)",
                        _prev_resp_id, *_expanded_counts)
        else:
            logger.warning("polyfill: %s not in cache, sending current input only", _prev_resp_id)

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
    if upstream.status_code < 400 and isinstance(data, dict):
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

    payload["model"] = used_cfg.model_id
    payload.pop("model_provider", None)

    upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/images/generations"
    headers = _build_upstream_headers(used_cfg)

    client = await get_http_client()
    t0 = time.monotonic()
    upstream = await client.post(upstream_url, json=payload, headers=headers)
    duration_ms = int((time.monotonic() - t0) * 1000)

    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)
    content_type = upstream.headers.get("content-type", "application/json")

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

    client = await get_http_client()
    response_headers: Dict[str, str] = {}

    should_use_direct_vertex = (
        public_model.provider_name.strip().lower() == "google"
        and bool(settings.vertex_api_key)
    )

    if should_use_direct_vertex:
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
        upstream = await client.post(upstream_url, json=payload, headers=headers)
        duration_ms = int((time.monotonic() - t0) * 1000)
        response_headers = filter_headers(dict(upstream.headers))
        response_headers.pop("content-length", None)
        try:
            upstream_data = upstream.json()
        except Exception:
            return JSONResponse(
                content={"error": {"message": "Vertex returned invalid JSON", "type": "server_error", "code": "upstream_invalid_json"}},
                status_code=502,
                headers=response_headers,
            )

        if upstream.status_code < 400:
            data = _translate_vertex_image_edit_response(upstream_data if isinstance(upstream_data, dict) else {})
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
        upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/images/edits"
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
            image_count=image_count,
            price_per_image_cents=public_model.price_per_image_cents,
        )
    elif isinstance(data, (dict, str)):
        logger.error("image edit upstream error %s: %s", upstream.status_code, str(data)[:500])

    if isinstance(data, dict):
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)
