import asyncio
import json
import logging
import time
from datetime import date, datetime
from types import SimpleNamespace
from typing import Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import settings
from .db import get_db
from .models import ApiKey, UsageDaily
from .rate_limiter import rate_limiter
from .security import extract_api_key, hash_key
from .router import extract_messages_for_routing_from_responses_payload
from .router import registry as model_registry
from .router import resolve as resolve_model
from .usage_buffer import extract_cached_tokens, usage_buffer

_KEY_KIND_ATTR = "_key_kind"


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
        _http_client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(60.0))
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
        stream_timeout = httpx.Timeout(connect=5.0, read=None, write=60.0, pool=60.0)
        _http_stream_client = httpx.AsyncClient(limits=limits, timeout=stream_timeout)
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


def filter_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


@router.get("/responses")
async def responses_health():
    return {"status": "ok"}


async def _resolve_user(request: Request, db: AsyncSession):
    """Resolve API key → user object. Identity + active + expiry check."""
    if not settings.upstream_api_key:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="upstream api key not configured")

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

    # Route (best-effort). If router is disabled or misconfigured, this resolves to premium.
    display_model = str(payload.get("model") or settings.fixed_model)
    messages_for_route, tools_for_route = extract_messages_for_routing_from_responses_payload(payload)
    model_cfg, route_reason = resolve_model(messages_for_route, tools_for_route)
    used_cfg = model_cfg
    used_route_reason = route_reason

    payload["model"] = used_cfg.model_id
    payload.pop("model_provider", None)
    base_payload = dict(payload)

    upstream_url = f"{used_cfg.upstream_url.rstrip('/')}/responses"
    headers = {
        "api-key": used_cfg.api_key,
        "content-type": "application/json",
    }

    _STRIP_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty",
                     "max_output_tokens", "n", "logprobs", "top_logprobs", "seed")

    if base_payload.get("stream"):
        model_registry.ensure_initialized()
        fallback_cfg = model_registry.models.get("fallback") or model_registry.get("premium")
        cheap_cfg = model_registry.models.get("cheap")
        is_cheap = bool(cheap_cfg and used_cfg.model_id == cheap_cfg.model_id)
        can_fallback = (used_cfg.model_id != fallback_cfg.model_id)

        stream_client = await get_stream_client()

        async def _send_stream(cfg):
            send_payload = dict(base_payload)
            send_payload["model"] = cfg.model_id
            if cfg.strip_unsupported:
                for param in _STRIP_PARAMS:
                    send_payload.pop(param, None)
            req_url = f"{cfg.upstream_url.rstrip('/')}/responses"
            req_headers = {"api-key": cfg.api_key, "content-type": "application/json"}
            req = stream_client.build_request("POST", req_url, json=send_payload, headers=req_headers)
            return await stream_client.send(req, stream=True)

        try:
            upstream = await _send_stream(used_cfg)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            if can_fallback:
                _fb = "cheap" if is_cheap else "premium"
                used_cfg = fallback_cfg
                used_route_reason = f"{_fb}_fallback_timeout"
                can_fallback = False
                is_cheap = False
                upstream = await _send_stream(used_cfg)
            else:
                logger.error("upstream stream connect error: %s", exc)
                return JSONResponse(
                    content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                    status_code=502,
                )

        if can_fallback and (upstream.status_code == 429 or upstream.status_code >= 500):
            try:
                await upstream.aclose()
            except Exception:
                pass
            _fb = "cheap" if is_cheap else "premium"
            used_cfg = fallback_cfg
            used_route_reason = f"{_fb}_fallback_429" if upstream.status_code == 429 else f"{_fb}_fallback_5xx"
            can_fallback = False
            is_cheap = False
            upstream = await _send_stream(used_cfg)

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
                upstream = await _send_stream(used_cfg)
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
                                except (json.JSONDecodeError, ValueError):
                                    pass
            finally:
                await upstream.aclose()
                if upstream.status_code < 400:
                    dur = int((time.monotonic() - stream_t0) * 1000)
                    asyncio.create_task(usage_buffer.add(
                        user.id,
                        input_tokens=_stream_usage["input"],
                        output_tokens=_stream_usage["output"],
                        cached_tokens=_stream_usage["cached"],
                        requests=1,
                        endpoint="responses:stream",
                        model=used_cfg.model_id,
                        route_reason=used_route_reason,
                        duration_ms=dur,
                        status_code=upstream.status_code,
                        price_input_per_million=used_cfg.price_input_per_million,
                        price_output_per_million=used_cfg.price_output_per_million,
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
    is_cheap = bool(cheap_cfg and used_cfg.model_id == cheap_cfg.model_id)
    can_fallback = (used_cfg.model_id != fallback_cfg.model_id)

    client = await get_http_client()

    async def _post_json(cfg):
        send_payload = dict(base_payload)
        send_payload["model"] = cfg.model_id
        if cfg.strip_unsupported:
            for param in _STRIP_PARAMS:
                send_payload.pop(param, None)
        req_url = f"{cfg.upstream_url.rstrip('/')}/responses"
        req_headers = {"api-key": cfg.api_key, "content-type": "application/json"}
        t0 = time.monotonic()
        r = await client.post(req_url, json=send_payload, headers=req_headers)
        dur = int((time.monotonic() - t0) * 1000)
        return r, dur

    try:
        upstream, duration_ms = await _post_json(used_cfg)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        if can_fallback:
            _fb = "cheap" if is_cheap else "premium"
            used_cfg = fallback_cfg
            used_route_reason = f"{_fb}_fallback_timeout"
            can_fallback = False
            is_cheap = False
            upstream, duration_ms = await _post_json(used_cfg)
        else:
            logger.error("upstream request error: %s", exc)
            return JSONResponse(
                content={"error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_unreachable"}},
                status_code=502,
            )

    if can_fallback and (upstream.status_code == 429 or upstream.status_code >= 500):
        _fb = "cheap" if is_cheap else "premium"
        used_cfg = fallback_cfg
        used_route_reason = f"{_fb}_fallback_429" if upstream.status_code == 429 else f"{_fb}_fallback_5xx"
        can_fallback = False
        is_cheap = False
        upstream, duration_ms = await _post_json(used_cfg)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    if can_fallback and "application/json" not in content_type:
        _fb = "cheap" if is_cheap else "premium"
        used_cfg = fallback_cfg
        used_route_reason = f"{_fb}_fallback_unexpected"
        can_fallback = False
        is_cheap = False
        upstream, duration_ms = await _post_json(used_cfg)
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
                upstream, duration_ms = await _post_json(used_cfg)
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

    if upstream.status_code < 400:
        await usage_buffer.add(
            user.id,
            input_tokens=input_tokens_delta,
            output_tokens=output_tokens_delta,
            cached_tokens=cached_tokens_delta,
            requests=1,
            endpoint="responses",
            model=used_cfg.model_id,
            route_reason=used_route_reason,
            duration_ms=duration_ms,
            status_code=upstream.status_code,
            price_input_per_million=used_cfg.price_input_per_million,
            price_output_per_million=used_cfg.price_output_per_million,
        )
    elif isinstance(data, (dict, str)):
        logger.error("upstream error %s: %s", upstream.status_code, str(data)[:500])

    if isinstance(data, dict):
        data["model"] = display_model
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)
