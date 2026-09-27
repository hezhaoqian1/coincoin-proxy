from __future__ import annotations

import asyncio
from typing import Any

from .config import settings


_redis_client: Any = None
_redis_lock = asyncio.Lock()


async def get_redis_client() -> Any:
    """Return the shared Redis client for optional quota/usage infrastructure."""
    global _redis_client
    if not settings.redis_url:
        raise RuntimeError("COINCOIN_REDIS_URL is not configured")
    if _redis_client is not None:
        return _redis_client

    async with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            from redis.asyncio import Redis
        except Exception as exc:
            raise RuntimeError("redis package is required for Redis-backed CoinCoin infrastructure") from exc
        # Bounded timeouts keep a hung Redis from stalling request handlers; the
        # shared-state helpers additionally wrap calls in a circuit breaker.
        options = {
            "decode_responses": True,
            "socket_connect_timeout": max(0.1, float(settings.redis_connect_timeout_seconds or 1.0)),
            "socket_timeout": max(0.1, float(settings.redis_socket_timeout_seconds or 2.0)),
            "socket_keepalive": True,
            "health_check_interval": max(0, int(settings.redis_health_check_interval_seconds or 0)),
        }
        max_connections = int(settings.redis_max_connections or 0)
        if max_connections > 0:
            options["max_connections"] = max_connections
        _redis_client = Redis.from_url(settings.redis_url, **options)
        return _redis_client


async def close_redis_client() -> None:
    global _redis_client
    client = _redis_client
    _redis_client = None
    if client is not None:
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
