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
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
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
