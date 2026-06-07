import asyncio
import logging
import time
from typing import Dict, Tuple

from .config import settings
from .redis_client import get_redis_client


logger = logging.getLogger("coincoin.rate_limiter")

_REDIS_FIXED_WINDOW_SCRIPT = """
local current = redis.call("INCR", KEYS[1])
if current == 1 then
  redis.call("EXPIRE", KEYS[1], ARGV[2])
end
if current > tonumber(ARGV[1]) then
  return 0
end
return 1
"""


class RateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._buckets: Dict[str, Tuple[int, int]] = {}

    async def allow(self, user_id: str, limit_per_minute: int) -> bool:
        if limit_per_minute <= 0:
            return False
        if settings.redis_rate_limiter_enabled:
            try:
                return await self._allow_redis(user_id, limit_per_minute)
            except Exception:
                logger.exception("redis rate limiter failed")
                if not settings.redis_rate_limiter_fallback_to_local:
                    return False
        now_min = int(time.time() // 60)
        async with self._lock:
            bucket = self._buckets.get(user_id)
            if not bucket or bucket[0] != now_min:
                self._buckets[user_id] = (now_min, 1)
                return True
            count = bucket[1] + 1
            if count > limit_per_minute:
                self._buckets[user_id] = (now_min, count)
                return False
            self._buckets[user_id] = (now_min, count)
            return True

    async def _allow_redis(self, user_id: str, limit_per_minute: int) -> bool:
        client = await get_redis_client()
        now_min = int(time.time() // 60)
        key = f"{settings.redis_key_prefix}:rate:v1:{user_id}:{now_min}"
        allowed = await client.eval(_REDIS_FIXED_WINDOW_SCRIPT, 1, key, int(limit_per_minute), 90)
        return bool(int(allowed or 0))


rate_limiter = RateLimiter()
