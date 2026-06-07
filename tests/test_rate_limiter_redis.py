import time
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.rate_limiter import RateLimiter


class _FakeRedis:
    def __init__(self):
        self.counts = {}
        self.expiries = {}

    async def eval(self, _script, _num_keys, key, limit, ttl_seconds):
        now = time.time()
        expires_at = self.expiries.get(key, 0)
        if expires_at <= now:
            self.counts[key] = 0
        self.counts[key] = self.counts.get(key, 0) + 1
        self.expiries[key] = now + int(ttl_seconds)
        return 1 if self.counts[key] <= int(limit) else 0


class RedisRateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._enabled = settings.redis_rate_limiter_enabled
        self._url = settings.redis_url
        self._fallback = settings.redis_rate_limiter_fallback_to_local
        settings.redis_rate_limiter_enabled = True
        settings.redis_url = "redis://example.invalid/0"
        settings.redis_rate_limiter_fallback_to_local = True

    async def asyncTearDown(self):
        settings.redis_rate_limiter_enabled = self._enabled
        settings.redis_url = self._url
        settings.redis_rate_limiter_fallback_to_local = self._fallback

    async def test_redis_limiter_shares_counts_across_instances(self):
        redis = _FakeRedis()

        with patch("app.rate_limiter.get_redis_client", AsyncMock(return_value=redis)):
            limiter_a = RateLimiter()
            limiter_b = RateLimiter()

            self.assertTrue(await limiter_a.allow("u_shared", 1))
            self.assertFalse(await limiter_b.allow("u_shared", 1))

    async def test_redis_limiter_falls_back_to_local_when_redis_unavailable(self):
        with patch("app.rate_limiter.get_redis_client", AsyncMock(side_effect=RuntimeError("redis down"))):
            limiter = RateLimiter()

            self.assertTrue(await limiter.allow("u_local", 1))
            self.assertFalse(await limiter.allow("u_local", 1))


if __name__ == "__main__":
    unittest.main()
