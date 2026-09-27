"""Multi-worker shared state: two in-process "workers" share one fake Redis.

Each test builds independent instances (caches, routers, locks) to stand in for
separate uvicorn worker processes and asserts they observe each other's state
through Redis, and that everything degrades to process-local behaviour when
Redis is absent or failing.
"""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import distributed_state
from app.channel_router import ChannelRouter, ModelChannelRouteSnapshot, ProviderChannelSnapshot
from app.config import settings
from app.distributed_state import (
    CacheInvalidationBus,
    LeaderLock,
    SharedCooldownRegistry,
    claim_once,
    drain_background_tasks,
    redis_guard,
)
from app.proxy import KeyCache, ResponseConversationCache


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return self

        return _record

    async def execute(self):
        results = []
        for name, args, kwargs in self._calls:
            results.append(await getattr(self._redis, name)(*args, **kwargs))
        self._calls = []
        return results


class FakeRedis:
    """Async Redis double covering the commands and Lua scripts used here."""

    def __init__(self):
        self.values = {}
        self.expires_ms = {}
        self.zsets = {}
        self.published = []
        self.fail = False
        self.calls = 0

    def _now_ms(self):
        return int(time.time() * 1000)

    def _check(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")

    def _alive(self, key):
        expires = self.expires_ms.get(key)
        if expires is not None and expires <= self._now_ms():
            self.values.pop(key, None)
            self.expires_ms.pop(key, None)
        return key in self.values

    async def ping(self):
        self._check()
        return True

    async def get(self, key):
        self._check()
        return self.values.get(key) if self._alive(key) else None

    async def set(self, key, value, nx=False, px=None):
        self._check()
        if nx and self._alive(key):
            return None
        self.values[key] = str(value)
        if px:
            self.expires_ms[key] = self._now_ms() + int(px)
        else:
            self.expires_ms.pop(key, None)
        return True

    async def delete(self, *keys):
        self._check()
        removed = 0
        for key in keys:
            if self._alive(key):
                removed += 1
            self.values.pop(key, None)
            self.expires_ms.pop(key, None)
        return removed

    async def pttl(self, key):
        self._check()
        if not self._alive(key):
            return -2
        expires = self.expires_ms.get(key)
        return -1 if expires is None else max(0, expires - self._now_ms())

    async def zadd(self, key, mapping):
        self._check()
        self.zsets.setdefault(key, {}).update({member: float(score) for member, score in mapping.items()})
        return len(mapping)

    async def zrem(self, key, *members):
        self._check()
        zset = self.zsets.get(key, {})
        return sum(1 for member in members if zset.pop(member, None) is not None)

    async def zrangebyscore(self, key, min_score, max_score, withscores=False):
        self._check()
        low = float(min_score)
        high = float("inf") if str(max_score) == "+inf" else float(max_score)
        rows = sorted(
            ((member, score) for member, score in self.zsets.get(key, {}).items() if low <= score <= high),
            key=lambda row: row[1],
        )
        return rows if withscores else [member for member, _ in rows]

    async def publish(self, channel, message):
        self._check()
        self.published.append((channel, message))
        return 1

    def pipeline(self, transaction=False):
        return _FakePipeline(self)

    async def eval(self, script, numkeys, *args):
        self._check()
        keys, argv = list(args[:numkeys]), list(args[numkeys:])
        if script is distributed_state._RECORD_FAILURE_SCRIPT:
            return self._record_failure(keys, argv)
        if script is distributed_state._RENEW_LOCK_SCRIPT:
            key, token, ttl_ms = keys[0], argv[0], int(argv[1])
            if self._alive(key) and self.values[key] == token:
                self.expires_ms[key] = self._now_ms() + ttl_ms
                return 1
            return 0
        if script is distributed_state._RELEASE_LOCK_SCRIPT:
            key, token = keys[0], argv[0]
            if self._alive(key) and self.values[key] == token:
                self.values.pop(key, None)
                self.expires_ms.pop(key, None)
                return 1
            return 0
        raise AssertionError("unexpected Lua script")

    def _record_failure(self, keys, argv):
        fails_key, zset_key = keys
        channel_id, allowed, cooldown_ms, now, counter_ttl = (
            argv[0],
            int(argv[1]),
            int(argv[2]),
            int(argv[3]),
            int(argv[4]),
        )
        zset = self.zsets.setdefault(zset_key, {})
        for member, score in list(zset.items()):
            if score <= now:
                zset.pop(member)
        current = zset.get(channel_id, 0)
        if current > now:
            return [0, int(current)]
        failures = int(self.values.get(fails_key, 0) if self._alive(fails_key) else 0) + 1
        self.values[fails_key] = str(failures)
        self.expires_ms[fails_key] = self._now_ms() + counter_ttl
        if failures >= allowed:
            self.values.pop(fails_key, None)
            self.expires_ms.pop(fails_key, None)
            if cooldown_ms > 0:
                until = now + cooldown_ms
                zset[channel_id] = until
                return [0, until]
            return [0, 0]
        return [failures, 0]


class _SharedRedisTestCase(unittest.IsolatedAsyncioTestCase):
    """Points all shared-state helpers at one FakeRedis ("the cluster Redis")."""

    redis_configured = True

    async def asyncSetUp(self):
        self.redis = FakeRedis()
        self._saved = {
            name: getattr(settings, name)
            for name in (
                "redis_url",
                "redis_rate_limiter_enabled",
                "response_cache_shared_enabled",
                "channel_state_shared_enabled",
                "key_cache_invalidation_enabled",
                "redis_circuit_failure_threshold",
                "redis_circuit_open_seconds",
                "fallback_alert_dedup_seconds",
            )
        }
        settings.redis_url = "redis://shared.example/0" if self.redis_configured else ""
        settings.redis_rate_limiter_enabled = None
        settings.response_cache_shared_enabled = None
        settings.channel_state_shared_enabled = None
        settings.key_cache_invalidation_enabled = None
        settings.redis_circuit_failure_threshold = 3
        settings.redis_circuit_open_seconds = 10.0
        redis_guard.reset()
        self._patch = patch("app.distributed_state.get_redis_client", AsyncMock(return_value=self.redis))
        self._patch.start()
        if self.redis_configured:
            self.assertTrue(await redis_guard.warm_up())

    async def asyncTearDown(self):
        await drain_background_tasks()
        self._patch.stop()
        for name, value in self._saved.items():
            setattr(settings, name, value)
        redis_guard.reset()


def _message(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _reply(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


class SharedResponseCacheTests(_SharedRedisTestCase):
    def _cache(self, **overrides):
        options = dict(ttl_seconds=300, max_entries=50, max_total_bytes=1024 * 1024, max_entry_bytes=64 * 1024, max_turns=8)
        options.update(overrides)
        return ResponseConversationCache(**options)

    async def test_follow_up_on_another_worker_restores_context(self):
        worker_a, worker_b = self._cache(), self._cache()
        await worker_a.aset("resp_1", [_message("hello")], [_reply("hi")], owner="u_1")

        restored = await worker_b.aget("resp_1", owner="u_1")

        self.assertIsNotNone(restored)
        expanded_input, output = restored
        self.assertEqual(expanded_input[0]["content"][0]["text"], "hello")
        self.assertEqual(output[0]["content"][0]["text"], "hi")
        self.assertEqual(worker_b.stats["l2_hits"], 1)
        # Second lookup is served from worker B's L1 without touching Redis.
        calls_before = self.redis.calls
        self.assertIsNotNone(await worker_b.aget("resp_1", owner="u_1"))
        self.assertEqual(self.redis.calls, calls_before)
        self.assertEqual(worker_b.stats["l1_hits"], 1)

    async def test_other_user_cannot_replay_response_id(self):
        worker_a, worker_b = self._cache(), self._cache()
        await worker_a.aset("resp_private", [_message("secret")], [_reply("ok")], owner="u_owner")

        self.assertIsNone(await worker_b.aget("resp_private", owner="u_attacker"))
        self.assertIsNone(await worker_a.aget("resp_private", owner="u_attacker"))
        self.assertEqual(worker_b.stats["owner_mismatch"], 1)
        self.assertEqual(worker_a.stats["owner_mismatch"], 1)
        self.assertIsNotNone(await worker_b.aget("resp_private", owner="u_owner"))

    async def test_large_entries_are_compressed_small_entries_stored_plain(self):
        cache = self._cache()
        await cache.aset("resp_small", [_message("tiny")], [], owner="u_1")
        await cache.aset("resp_big", [_message("codex " * 4000)], [_reply("done")], owner="u_1")

        small_raw = self.redis.values[ResponseConversationCache._redis_key("resp_small")]
        big_raw = self.redis.values[ResponseConversationCache._redis_key("resp_big")]
        self.assertTrue(small_raw.startswith("cc1|j|"))
        self.assertTrue(big_raw.startswith("cc1|z|"))
        self.assertLess(len(big_raw), len("codex " * 4000))

        restored = await self._cache().aget("resp_big", owner="u_1")
        self.assertEqual(restored[0][0]["content"][0]["text"], "codex " * 4000)

    async def test_entry_too_big_for_l1_still_shared_through_redis(self):
        worker_a = self._cache(max_entry_bytes=2048)
        worker_b = self._cache(max_entry_bytes=2048)
        long_history = [_message("x" * 5000)]
        await worker_a.aset("resp_long", long_history, [_reply("ok")], owner="u_1")

        self.assertIsNone(worker_a.get("resp_long"))
        restored = await worker_b.aget("resp_long", owner="u_1")
        self.assertIsNotNone(restored)
        self.assertEqual(worker_b.stats["l2_hits"], 1)

    async def test_entries_over_shared_budget_are_skipped(self):
        saved = settings.response_cache_redis_max_entry_bytes
        settings.response_cache_redis_max_entry_bytes = 1024
        try:
            cache = self._cache(max_entry_bytes=1024)
            await cache.aset("resp_huge", [_message("y" * 4096)], [], owner="u_1")
        finally:
            settings.response_cache_redis_max_entry_bytes = saved
        self.assertNotIn(ResponseConversationCache._redis_key("resp_huge"), self.redis.values)
        self.assertEqual(cache.stats["l2_skipped_oversize"], 1)

    async def test_redis_ttl_bounds_local_copy(self):
        await self._cache().aset("resp_ttl", [_message("a")], [], owner="u_1")
        key = ResponseConversationCache._redis_key("resp_ttl")
        self.redis.expires_ms[key] = int(time.time() * 1000) + 1500
        worker_b = self._cache()

        await worker_b.aget("resp_ttl", owner="u_1")

        expires_at = worker_b._data["resp_ttl"][0]
        self.assertLessEqual(expires_at - time.time(), 2.0)

    async def test_redis_outage_degrades_to_local_and_opens_breaker(self):
        worker_a, worker_b = self._cache(), self._cache()
        self.redis.fail = True

        await worker_a.aset("resp_down", [_message("a")], [], owner="u_1")
        self.assertIsNotNone(await worker_a.aget("resp_down", owner="u_1"))  # L1 still works
        for _ in range(3):
            self.assertIsNone(await worker_b.aget("resp_down", owner="u_1"))
            await drain_background_tasks()
            if redis_guard._warm_task is not None:
                await asyncio.gather(redis_guard._warm_task, return_exceptions=True)
        self.assertTrue(redis_guard.is_open())

        calls_before = self.redis.calls
        self.assertIsNone(await worker_b.aget("resp_down", owner="u_1"))
        self.assertEqual(self.redis.calls, calls_before, "open breaker must not touch Redis")

    async def test_corrupt_shared_entry_is_ignored(self):
        self.redis.values[ResponseConversationCache._redis_key("resp_bad")] = "garbage"
        cache = self._cache()
        self.assertIsNone(await cache.aget("resp_bad", owner="u_1"))
        self.assertEqual(cache.stats["l2_errors"], 1)


class RedisGuardWarmUpTests(_SharedRedisTestCase):
    async def test_cold_pool_is_not_used_on_request_path(self):
        redis_guard.reset()  # cold again
        calls_before = self.redis.calls
        ok, _ = await redis_guard.call("probe", lambda client: client.get("k"))
        self.assertFalse(ok, "cold guard must not dial on the caller's time")
        self.assertEqual(self.redis.calls, calls_before)
        # The background warm-up it scheduled makes the next call succeed.
        await asyncio.gather(redis_guard._warm_task, return_exceptions=True)
        self.assertTrue(redis_guard.is_warm())
        ok, _ = await redis_guard.call("probe", lambda client: client.get("k"))
        self.assertTrue(ok)

    async def test_slow_dial_does_not_starve_operations(self):
        """A dial slower than the op timeout still completes during warm-up."""

        class _SlowDial(FakeRedis):
            def __init__(self):
                super().__init__()
                self.dialed = False

            async def ping(self):
                if not self.dialed:
                    await asyncio.sleep(0.3)  # > op timeout, < connect budget
                    self.dialed = True
                return True

        slow = _SlowDial()
        saved = settings.redis_op_timeout_seconds
        settings.redis_op_timeout_seconds = 0.05
        try:
            redis_guard.reset()
            with patch("app.distributed_state.get_redis_client", AsyncMock(return_value=slow)):
                self.assertTrue(await redis_guard.warm_up())
                ok, _ = await redis_guard.call("probe", lambda client: client.get("k"))
        finally:
            settings.redis_op_timeout_seconds = saved
        self.assertTrue(ok)

    async def test_failure_marks_pool_cold_and_rewarms(self):
        self.redis.fail = True
        ok, _ = await redis_guard.call("probe", lambda client: client.get("k"))
        self.assertFalse(ok)
        self.assertFalse(redis_guard.is_warm())
        self.redis.fail = False
        self.assertFalse(redis_guard.available())  # schedules warm-up
        await asyncio.gather(redis_guard._warm_task, return_exceptions=True)
        self.assertTrue(redis_guard.available())


class LocalOnlyResponseCacheTests(_SharedRedisTestCase):
    redis_configured = False

    async def test_without_redis_cache_is_process_local(self):
        worker_a = ResponseConversationCache(ttl_seconds=300)
        worker_b = ResponseConversationCache(ttl_seconds=300)
        await worker_a.aset("resp_local", [_message("a")], [], owner="u_1")

        self.assertIsNotNone(await worker_a.aget("resp_local", owner="u_1"))
        self.assertIsNone(await worker_b.aget("resp_local", owner="u_1"))
        self.assertEqual(self.redis.calls, 0)


def _router(namespace):
    router = ChannelRouter(shared_namespace=namespace)
    router.set_snapshot(
        [
            ProviderChannelSnapshot(
                channel_id="ch_a",
                name="A",
                base_url="https://a.example/v1",
                api_key="k",
                status="active",
                priority=0,
                weight=1,
                allowed_fails=2,
                cooldown_seconds=30,
            ),
            ProviderChannelSnapshot(
                channel_id="ch_b",
                name="B",
                base_url="https://b.example/v1",
                api_key="k",
                status="active",
                priority=0,
                weight=1,
                allowed_fails=2,
                cooldown_seconds=30,
            ),
        ],
        [
            ModelChannelRouteSnapshot(route_id="r_a", public_model_id="gpt-5.5", channel_id="ch_a", endpoint="responses", status="active"),
            ModelChannelRouteSnapshot(route_id="r_b", public_model_id="gpt-5.5", channel_id="ch_b", endpoint="responses", status="active"),
        ],
    )
    return router


class _Model:
    public_id = "gpt-5.5"
    route_public_ids = ()


class SharedChannelCooldownTests(_SharedRedisTestCase):
    def _pick(self, router, affinity):
        choice = router.select_for_model(_Model(), None, "responses", affinity_key=affinity)
        return choice.channel_id if choice else None

    def _affinity_for(self, router, channel_id):
        for index in range(200):
            key = f"aff-{index}"
            if self._pick(router, key) == channel_id:
                return key
        self.fail(f"no affinity key maps to {channel_id}")

    async def test_cooldown_on_one_worker_reroutes_same_conversation_on_all_workers(self):
        worker_a, worker_b = _router("t_router_1"), _router("t_router_1")
        affinity = self._affinity_for(worker_a, "ch_a")
        self.assertEqual(self._pick(worker_b, affinity), "ch_a")

        worker_a.record_failure("ch_a")
        worker_a.record_failure("ch_a")
        await drain_background_tasks()
        await worker_b._shared.sync_once()

        # Both workers now agree, so the conversation stays on one channel.
        self.assertEqual(self._pick(worker_a, affinity), "ch_b")
        self.assertEqual(self._pick(worker_b, affinity), "ch_b")

    async def test_failures_are_counted_across_workers(self):
        worker_a, worker_b = _router("t_router_2"), _router("t_router_2")
        worker_a.record_failure("ch_a")
        worker_b.record_failure("ch_a")  # each worker alone is below allowed_fails=2
        await drain_background_tasks()
        await worker_a._shared.sync_once()
        await worker_b._shared.sync_once()

        self.assertGreater(worker_a.channel_state("ch_a")["cooldown_until"], time.time())
        self.assertGreater(worker_b.channel_state("ch_a")["cooldown_until"], time.time())

    async def test_success_elsewhere_lifts_shared_cooldown(self):
        worker_a, worker_b = _router("t_router_3"), _router("t_router_3")
        worker_a.record_failure("ch_a")
        worker_a.record_failure("ch_a")
        await drain_background_tasks()
        await worker_b._shared.sync_once()
        self.assertGreater(worker_b.channel_state("ch_a")["cooldown_until"], time.time())

        worker_b.record_success("ch_a")
        await drain_background_tasks()
        await worker_a._shared.sync_once()

        self.assertLessEqual(worker_a.channel_state("ch_a").get("cooldown_until", 0), time.time())

    async def test_late_failures_do_not_extend_active_cooldown(self):
        worker = _router("t_router_4")
        worker.record_failure("ch_a")
        worker.record_failure("ch_a")
        await drain_background_tasks()
        zset_key = worker._shared._zset_key()
        first_until = self.redis.zsets[zset_key]["ch_a"]

        worker.record_failure("ch_a")
        worker.record_failure("ch_a")
        await drain_background_tasks()

        self.assertEqual(self.redis.zsets[zset_key]["ch_a"], first_until)

    async def test_admin_reset_clears_cluster_state(self):
        worker_a, worker_b = _router("t_router_5"), _router("t_router_5")
        worker_a.record_failure("ch_a")
        worker_a.record_failure("ch_a")
        await drain_background_tasks()
        await worker_b._shared.sync_once()

        worker_a.reset_channel_state("ch_a")
        await drain_background_tasks()
        await worker_b._shared.sync_once()

        self.assertLessEqual(worker_b.channel_state("ch_a").get("cooldown_until", 0), time.time())

    async def test_local_cooldown_during_outage_is_published_after_recovery(self):
        worker_a, worker_b = _router("t_router_6"), _router("t_router_6")
        self.redis.fail = True
        worker_a.record_failure("ch_a")
        worker_a.record_failure("ch_a")
        await drain_background_tasks()
        # Local routing still honours the cooldown while Redis is down.
        self.assertGreater(worker_a.channel_state("ch_a")["cooldown_until"], time.time())

        self.redis.fail = False
        redis_guard.reset()
        self.assertTrue(await redis_guard.warm_up())
        await worker_a._shared.sync_once()
        await worker_b._shared.sync_once()

        self.assertGreater(worker_b.channel_state("ch_a")["cooldown_until"], time.time())

    async def test_router_without_namespace_stays_local(self):
        router = ChannelRouter()
        router.set_snapshot([], [])
        calls_before = self.redis.calls
        router.record_success("ch_x")
        await drain_background_tasks()
        self.assertEqual(self.redis.calls, calls_before)


class GeminiCpaSharedCooldownTests(_SharedRedisTestCase):
    async def test_gemini_cooldown_propagates(self):
        from app import gemini_cpa

        channel = gemini_cpa.GeminiCpaChannel(
            channel_id="cpa_shared_test",
            public_id="gemini-3",
            provider_model="gemini-3",
            upstream_url="https://cpa.example/v1",
            api_key="k",
            auth_style="bearer",
            priority=0,
            weight=1,
            allowed_fails=1,
            cooldown_seconds=30,
        )
        gemini_cpa.record_failure(channel)
        await drain_background_tasks()
        gemini_cpa._CHANNEL_STATE.pop(channel.channel_id, None)  # simulate a sibling worker

        await gemini_cpa._SHARED_COOLDOWNS.sync_once()

        self.assertFalse(gemini_cpa._is_available(channel))
        gemini_cpa.record_success(channel)
        await drain_background_tasks()


class KeyCacheInvalidationTests(_SharedRedisTestCase):
    async def test_delete_broadcasts_and_sibling_worker_drops_entry(self):
        bus = distributed_state.invalidation_bus
        worker_a = KeyCache(ttl_seconds=60, max_size=100)
        worker_b = KeyCache(ttl_seconds=60, max_size=100)
        await worker_a.set("hash_1", {"id": "u_1"})
        await worker_b.set("hash_1", {"id": "u_1"})

        await worker_a.delete("hash_1")

        self.assertIsNone(await worker_a.get("hash_1"))
        channel, raw = self.redis.published[-1]
        self.assertEqual(channel, bus.channel())
        message = json.loads(raw)
        self.assertEqual(message["kind"], KeyCache.INVALIDATION_KIND)

        sibling_bus = CacheInvalidationBus()
        sibling_bus.register(KeyCache.INVALIDATION_KIND, worker_b.delete_local)
        await sibling_bus.handle_message(json.dumps({**message, "origin": "another-worker"}))
        self.assertIsNone(await worker_b.get("hash_1"))

    async def test_worker_ignores_its_own_messages(self):
        cache = KeyCache(ttl_seconds=60, max_size=100)
        await cache.set("hash_2", {"id": "u_2"})
        bus = CacheInvalidationBus()
        bus.register(KeyCache.INVALIDATION_KIND, cache.delete_local)

        await bus.handle_message(
            json.dumps({"kind": KeyCache.INVALIDATION_KIND, "key": "hash_2", "origin": distributed_state.WORKER_ID})
        )

        self.assertIsNotNone(await cache.get("hash_2"))

    async def test_resubscribe_clears_local_cache(self):
        cache = KeyCache(ttl_seconds=60, max_size=100)
        await cache.set("hash_3", {"id": "u_3"})
        bus = CacheInvalidationBus()
        bus.register(KeyCache.INVALIDATION_KIND, cache.delete_local, on_resubscribe=cache.clear_local)

        await bus._on_subscribed()

        self.assertIsNone(await cache.get("hash_3"))


class LeaderLockTests(_SharedRedisTestCase):
    async def test_only_one_worker_leads_and_lease_is_renewed(self):
        lock_a, lock_b = LeaderLock("job", 30), LeaderLock("job", 30)

        self.assertTrue(await lock_a.acquire())
        self.assertFalse(await lock_b.acquire())
        self.assertTrue(await lock_a.acquire())  # renew keeps leadership

        await lock_a.release()
        self.assertTrue(await lock_b.acquire())

    async def test_redis_failure_fails_open_for_idempotent_jobs(self):
        self.redis.fail = True
        self.assertTrue(await LeaderLock("job2", 30).acquire())
        self.assertFalse(await LeaderLock("job3", 30, fail_open=False).acquire())


class LeaderLockWithoutRedisTests(_SharedRedisTestCase):
    redis_configured = False

    async def test_without_redis_every_worker_runs(self):
        self.assertTrue(await LeaderLock("job", 30).acquire())
        self.assertTrue(await LeaderLock("job", 30).acquire())


class ClaimOnceTests(_SharedRedisTestCase):
    async def test_claim_once_is_cluster_wide(self):
        self.assertTrue(await claim_once("alert", "k1", 60))
        self.assertFalse(await claim_once("alert", "k1", 60))
        self.assertTrue(await claim_once("alert", "k2", 60))

    async def test_fallback_alert_sent_once_across_workers(self):
        from app import fallback_alerts

        settings.fallback_alert_dedup_seconds = 300
        alert = fallback_alerts.FallbackExhaustedAlert(endpoint="responses", model="gpt-5.5", reason="exhausted", status_code=503)
        with patch("app.fallback_alerts._send_dingtalk_alert", AsyncMock(return_value=True)) as send:
            first = await fallback_alerts._send_dingtalk_alert_cluster_once(alert)
            second = await fallback_alerts._send_dingtalk_alert_cluster_once(alert)
        self.assertTrue(first)
        self.assertFalse(second)
        send.assert_awaited_once()


class RateLimiterAutoRedisTests(_SharedRedisTestCase):
    async def test_redis_limiter_auto_enabled_when_redis_configured(self):
        from app.rate_limiter import RateLimiter

        class _Counter:
            def __init__(self):
                self.counts = {}

            async def eval(self, _script, _num_keys, key, limit, _ttl):
                self.counts[key] = self.counts.get(key, 0) + 1
                return 1 if self.counts[key] <= int(limit) else 0

        shared = _Counter()
        with patch("app.rate_limiter.get_redis_client", AsyncMock(return_value=shared)):
            self.assertTrue(await RateLimiter().allow("u_auto", 1))
            self.assertFalse(await RateLimiter().allow("u_auto", 1))

    async def test_explicit_false_keeps_local_limiter(self):
        from app.rate_limiter import RateLimiter

        settings.redis_rate_limiter_enabled = False
        client = AsyncMock()
        with patch("app.rate_limiter.get_redis_client", AsyncMock(return_value=client)):
            limiter = RateLimiter()
            self.assertTrue(await limiter.allow("u_local_only", 1))
        client.eval.assert_not_called()


class SettingsFlagTests(unittest.TestCase):
    def test_blank_or_auto_flag_means_auto(self):
        from app.config import Settings

        with patch.dict(
            "os.environ",
            {
                "COINCOIN_CHANNEL_STATE_SHARED_ENABLED": "",
                "COINCOIN_RESPONSE_CACHE_SHARED_ENABLED": "auto",
                "COINCOIN_REDIS_RATE_LIMITER_ENABLED": "false",
            },
        ):
            loaded = Settings()
        self.assertIsNone(loaded.channel_state_shared_enabled)
        self.assertIsNone(loaded.response_cache_shared_enabled)
        self.assertIs(loaded.redis_rate_limiter_enabled, False)


class UsageEventStreamTrimTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_events_are_published_with_approximate_maxlen(self):
        from app import usage_events

        client = AsyncMock()
        saved = settings.usage_event_stream_maxlen
        settings.usage_event_stream_maxlen = 1234
        try:
            event = usage_events.build_usage_event(
                {"id": "rl_1", "user_id": "u_1", "created_at": time.time(), "cost_cents": 1}
            )
            with patch("app.usage_events.get_redis_client", AsyncMock(return_value=client)):
                await usage_events.UsageEventPublisher().publish(event)
        finally:
            settings.usage_event_stream_maxlen = saved
        _, kwargs = client.xadd.await_args
        self.assertEqual(kwargs, {"maxlen": 1234, "approximate": True})


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    def __init__(self, dialect, lock_result=1):
        self.dialect = type("D", (), {"name": dialect})()
        self.statements = []
        self._lock_result = lock_result

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        return _FakeResult(self._lock_result if "GET_LOCK" in sql else 1)


class StartupMigrationLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_mysql_lock_wraps_block_and_releases(self):
        from app.db import mysql_named_lock

        conn = _FakeConn("mysql")
        async with mysql_named_lock(conn, "coincoin:startup-migrations", 30) as acquired:
            self.assertTrue(acquired)
            conn.statements.append(("DDL", None))
        sql = [statement for statement, _ in conn.statements]
        self.assertIn("GET_LOCK", sql[0])
        self.assertEqual(sql[1], "DDL")
        self.assertIn("RELEASE_LOCK", sql[2])

    async def test_lock_timeout_proceeds_without_release(self):
        from app.db import mysql_named_lock

        conn = _FakeConn("mysql", lock_result=0)
        async with mysql_named_lock(conn, "coincoin:startup-migrations", 1) as acquired:
            self.assertFalse(acquired)
        self.assertFalse(any("RELEASE_LOCK" in statement for statement, _ in conn.statements))

    async def test_non_mysql_dialect_is_a_no_op(self):
        from app.db import mysql_named_lock

        conn = _FakeConn("sqlite")
        async with mysql_named_lock(conn, "x", 1) as acquired:
            self.assertFalse(acquired)
        self.assertEqual(conn.statements, [])


if __name__ == "__main__":
    unittest.main()
