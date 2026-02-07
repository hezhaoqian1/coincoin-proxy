import asyncio
import time
from typing import Dict, Tuple


class RateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._buckets: Dict[str, Tuple[int, int]] = {}

    async def allow(self, user_id: str, limit_per_minute: int) -> bool:
        if limit_per_minute <= 0:
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


rate_limiter = RateLimiter()
