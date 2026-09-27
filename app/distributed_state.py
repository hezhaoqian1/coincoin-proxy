"""Cross-worker runtime state backed by Redis.

CoinCoin runs several uvicorn worker processes (``WEB_CONCURRENCY``). Anything
kept only in process memory is invisible to sibling workers, which silently
breaks features that assume a single process: channel cooldowns diverge (the
same conversation bounces between upstream channels and loses prompt-cache
hits), API-key revocations only reach one worker, singleton jobs run N times
and alert dedup sends N copies.

This module provides the Redis-backed building blocks used to share that state.
It follows the same patterns as sub2api / new-api:

* ``RedisGuard``: every Redis call on a request path goes through short
  timeouts plus a circuit breaker, so a slow or unavailable Redis degrades to
  process-local behaviour instead of adding latency to user requests.
* ``SharedCooldownRegistry``: channel failure counting and cooldown windows
  kept in Redis (atomic Lua) and mirrored into each worker's local state by a
  sync loop. Local state stays authoritative for the synchronous hot path.
* ``CacheInvalidationBus``: Redis pub/sub fan-out for local cache invalidation
  (sub2api ``auth:cache:invalidate``).
* ``LeaderLock``: token-checked ``SET NX PX`` lock so singleton background jobs
  run on one worker at a time (sub2api ``leader:lock``).
* ``claim_once``: cluster-wide ``SET NX PX`` dedup.

When ``COINCOIN_REDIS_URL`` is not configured every helper behaves exactly like
the previous single-process implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .config import settings
from .redis_client import get_redis_client


logger = logging.getLogger("coincoin.distributed_state")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def redis_enabled() -> bool:
    return bool(str(settings.redis_url or "").strip())


def _auto_flag(flag: Optional[bool]) -> bool:
    """``None`` means "on whenever Redis is configured"; explicit False disables."""
    if flag is None:
        return redis_enabled()
    return bool(flag) and redis_enabled()


def channel_state_shared() -> bool:
    return _auto_flag(settings.channel_state_shared_enabled)


def response_cache_shared() -> bool:
    return _auto_flag(settings.response_cache_shared_enabled)


def key_cache_invalidation_shared() -> bool:
    return _auto_flag(settings.key_cache_invalidation_enabled)


def redis_rate_limiter_active() -> bool:
    return _auto_flag(settings.redis_rate_limiter_enabled)


def redis_key(*parts: Any) -> str:
    prefix = (str(settings.redis_key_prefix or "").strip() or "coincoin").rstrip(":")
    return ":".join([prefix, *(str(part) for part in parts if str(part) != "")])


# ---------------------------------------------------------------------------
# Redis guard: timeouts + circuit breaker
# ---------------------------------------------------------------------------


class RedisGuard:
    """Runs Redis operations with a timeout and a consecutive-failure breaker.

    After ``failure_threshold`` consecutive failures the breaker opens for
    ``open_seconds``; while open, ``call`` returns immediately without touching
    Redis so request latency is unaffected by a Redis outage. The first call
    after the window acts as a half-open probe.
    """

    def __init__(
        self,
        *,
        failure_threshold: Optional[int] = None,
        open_seconds: Optional[float] = None,
        default_timeout: Optional[float] = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._default_timeout = default_timeout
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._last_error_log = 0.0

    @property
    def failure_threshold(self) -> int:
        value = self._failure_threshold if self._failure_threshold is not None else settings.redis_circuit_failure_threshold
        return max(1, int(value or 1))

    @property
    def open_seconds(self) -> float:
        value = self._open_seconds if self._open_seconds is not None else settings.redis_circuit_open_seconds
        return max(0.0, float(value or 0.0))

    @property
    def default_timeout(self) -> float:
        value = self._default_timeout if self._default_timeout is not None else settings.redis_op_timeout_seconds
        return max(0.01, float(value or 0.25))

    def is_open(self, now: Optional[float] = None) -> bool:
        return self._open_until > (time.monotonic() if now is None else now)

    def available(self) -> bool:
        return redis_enabled() and not self.is_open()

    def reset(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0

    def record_success(self) -> None:
        if self._consecutive_failures or self._open_until:
            if self._open_until:
                logger.info("redis circuit closed; shared state restored")
            self._consecutive_failures = 0
            self._open_until = 0.0

    def record_failure(self, op_name: str, exc: BaseException) -> None:
        self._consecutive_failures += 1
        now = time.monotonic()
        if self._consecutive_failures >= self.failure_threshold and not self.is_open(now):
            self._open_until = now + self.open_seconds
            logger.warning(
                "redis circuit opened for %.1fs after %d failures (last op=%s error=%s); using process-local state",
                self.open_seconds,
                self._consecutive_failures,
                op_name,
                type(exc).__name__,
            )
            return
        if now - self._last_error_log >= 30:
            self._last_error_log = now
            logger.warning("redis op failed op=%s error=%s: %s", op_name, type(exc).__name__, exc)

    async def call(
        self,
        op_name: str,
        fn: Callable[[Any], Awaitable[Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Tuple[bool, Any]:
        """Run ``fn(client)``; returns ``(ok, result)``. Never raises except on cancellation."""
        if not self.available():
            return False, None
        try:
            client = await get_redis_client()
            result = await asyncio.wait_for(fn(client), timeout=timeout or self.default_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any Redis/transport error degrades to local state
            self.record_failure(op_name, exc)
            return False, None
        self.record_success()
        return True, result


redis_guard = RedisGuard()


# ---------------------------------------------------------------------------
# Background task tracking for fire-and-forget Redis writes
# ---------------------------------------------------------------------------

_BACKGROUND_TASKS: Set[asyncio.Task] = set()
_MAX_BACKGROUND_TASKS = 2048


def spawn_background(coro: Awaitable[Any], *, name: str = "") -> bool:
    """Schedule ``coro`` on the running loop, keeping a strong reference.

    Returns False (and closes the coroutine) when there is no running loop or
    too many writes are already pending, so callers on sync paths never block.
    """
    if len(_BACKGROUND_TASKS) >= _MAX_BACKGROUND_TASKS:
        _close_coro(coro)
        logger.warning("shared state background queue full; dropping %s", name or "task")
        return False
    try:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
    except RuntimeError:
        _close_coro(coro)
        return False
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return True


def _close_coro(coro: Any) -> None:
    close = getattr(coro, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


async def drain_background_tasks(timeout: float = 2.0) -> None:
    pending = [task for task in _BACKGROUND_TASKS if not task.done()]
    if not pending:
        return
    await asyncio.wait(pending, timeout=timeout)


# ---------------------------------------------------------------------------
# Shared channel cooldowns
# ---------------------------------------------------------------------------

# KEYS[1] = failure counter, KEYS[2] = cooldown zset (member=channel, score=until_ms)
# ARGV: channel_id, allowed_fails, cooldown_ms, now_ms, counter_ttl_ms
# Returns {failures_after, cooldown_until_ms}. While a channel is already cooling
# down, extra failures are not counted so late in-flight errors cannot extend or
# re-trigger the window.
_RECORD_FAILURE_SCRIPT = """
local now = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local current = tonumber(redis.call('ZSCORE', KEYS[2], ARGV[1]) or '0')
if current > now then
  return {0, current}
end
local allowed = tonumber(ARGV[2])
local cooldown_ms = tonumber(ARGV[3])
local failures = redis.call('INCR', KEYS[1])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
if failures >= allowed then
  redis.call('DEL', KEYS[1])
  if cooldown_ms > 0 then
    local until_ms = now + cooldown_ms
    redis.call('ZADD', KEYS[2], until_ms, ARGV[1])
    return {0, until_ms}
  end
  return {0, 0}
end
return {failures, 0}
"""


@dataclass
class _CooldownSyncState:
    remote_until: Dict[str, float] = field(default_factory=dict)
    local_only_until: Dict[str, float] = field(default_factory=dict)
    last_success_publish: Dict[str, float] = field(default_factory=dict)
    last_sync_ok: float = 0.0


class SharedCooldownRegistry:
    """Mirrors per-channel failure counting and cooldowns through Redis.

    ``apply_cooldown(channel_id, until_epoch_seconds)`` and
    ``clear_cooldown(channel_id)`` are callbacks into the owner's local state
    (``ChannelRouter`` / Gemini CPA). The owner keeps making synchronous routing
    decisions from local state; this registry only publishes local events and
    merges remote cooldowns set by sibling workers.
    """

    def __init__(
        self,
        namespace: str,
        *,
        apply_cooldown: Callable[[str, float], None],
        clear_cooldown: Callable[[str], None],
        guard: Optional[RedisGuard] = None,
    ) -> None:
        self.namespace = namespace
        self._apply_cooldown = apply_cooldown
        self._clear_cooldown = clear_cooldown
        self._guard = guard or redis_guard
        self._state = _CooldownSyncState()

    def _zset_key(self) -> str:
        return redis_key("chanstate", "v1", self.namespace, "cooldowns")

    def _failures_key(self, channel_id: str) -> str:
        return redis_key("chanstate", "v1", self.namespace, "fails", channel_id)

    @staticmethod
    def _counter_ttl_ms(cooldown_seconds: float) -> int:
        configured = max(1, int(settings.channel_state_failure_window_seconds or 600))
        return int(max(configured, float(cooldown_seconds or 0) * 4) * 1000)

    # -- publishing (called from sync hot paths) ---------------------------

    def record_failure(self, channel_id: str, *, allowed_fails: int, cooldown_seconds: float) -> None:
        if not channel_id or not channel_state_shared():
            return
        spawn_background(
            self._publish_failure(channel_id, max(1, int(allowed_fails or 1)), max(0.0, float(cooldown_seconds or 0))),
            name=f"{self.namespace}:failure",
        )

    def note_local_cooldown(self, channel_id: str, until: float) -> None:
        """Remember a cooldown decided locally in case Redis could not confirm it."""
        if channel_id and until > time.time():
            self._state.local_only_until[channel_id] = until

    def record_success(self, channel_id: str) -> None:
        if not channel_id or not channel_state_shared():
            return
        self._state.local_only_until.pop(channel_id, None)
        now = time.monotonic()
        min_interval = max(0.0, float(settings.channel_state_success_publish_interval_seconds or 0))
        known_remote = channel_id in self._state.remote_until
        last = self._state.last_success_publish.get(channel_id, 0.0)
        if not known_remote and now - last < min_interval:
            return
        self._state.last_success_publish[channel_id] = now
        spawn_background(self._publish_success(channel_id), name=f"{self.namespace}:success")

    def reset(self, channel_id: str) -> None:
        if not channel_id:
            return
        self._state.local_only_until.pop(channel_id, None)
        self._state.remote_until.pop(channel_id, None)
        if channel_state_shared():
            spawn_background(self._publish_success(channel_id), name=f"{self.namespace}:reset")

    async def _publish_failure(self, channel_id: str, allowed_fails: int, cooldown_seconds: float) -> None:
        now_ms = int(time.time() * 1000)
        cooldown_ms = int(cooldown_seconds * 1000)
        ok, result = await self._guard.call(
            f"{self.namespace}.record_failure",
            lambda client: client.eval(
                _RECORD_FAILURE_SCRIPT,
                2,
                self._failures_key(channel_id),
                self._zset_key(),
                channel_id,
                allowed_fails,
                cooldown_ms,
                now_ms,
                self._counter_ttl_ms(cooldown_seconds),
            ),
        )
        if not ok or not result:
            return
        try:
            until_ms = float(result[1] or 0)
        except (TypeError, ValueError, IndexError):
            return
        if until_ms > now_ms:
            until = until_ms / 1000.0
            self._state.remote_until[channel_id] = until
            self._state.local_only_until.pop(channel_id, None)
            self._apply_cooldown(channel_id, until)

    async def _publish_success(self, channel_id: str) -> None:
        async def _clear(client: Any) -> Any:
            pipe = client.pipeline(transaction=False)
            pipe.delete(self._failures_key(channel_id))
            pipe.zrem(self._zset_key(), channel_id)
            return await pipe.execute()

        ok, _ = await self._guard.call(f"{self.namespace}.record_success", _clear)
        if ok:
            self._state.remote_until.pop(channel_id, None)

    # -- sync loop ---------------------------------------------------------

    async def sync_once(self) -> bool:
        """Pull active cooldowns from Redis and reconcile local state."""
        now = time.time()
        now_ms = int(now * 1000)

        async def _fetch(client: Any) -> Any:
            return await client.zrangebyscore(self._zset_key(), now_ms, "+inf", withscores=True)

        ok, rows = await self._guard.call(f"{self.namespace}.sync", _fetch)
        if not ok:
            return False
        remote: Dict[str, float] = {}
        for member, score in rows or []:
            channel_id = member.decode() if isinstance(member, bytes) else str(member)
            remote[channel_id] = float(score) / 1000.0

        previous = self._state.remote_until
        for channel_id, until in remote.items():
            self._apply_cooldown(channel_id, until)
        for channel_id in set(previous) - set(remote):
            local_only = self._state.local_only_until.get(channel_id, 0.0)
            if local_only <= now:
                self._clear_cooldown(channel_id)

        # Local cooldowns that never reached Redis (breaker was open) are pushed
        # once Redis is healthy again so sibling workers learn about them.
        for channel_id, until in list(self._state.local_only_until.items()):
            if until <= now:
                self._state.local_only_until.pop(channel_id, None)
            elif channel_id not in remote:
                await self._republish_cooldown(channel_id, until)
                self._state.local_only_until.pop(channel_id, None)

        self._state.remote_until = remote
        self._state.last_sync_ok = now
        return True

    async def _republish_cooldown(self, channel_id: str, until: float) -> None:
        until_ms = int(until * 1000)
        await self._guard.call(
            f"{self.namespace}.republish",
            lambda client: client.zadd(self._zset_key(), {channel_id: until_ms}),
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "namespace": self.namespace,
            "remote_cooldowns": dict(self._state.remote_until),
            "local_only_cooldowns": dict(self._state.local_only_until),
            "last_sync_ok": self._state.last_sync_ok,
        }


_COOLDOWN_REGISTRIES: List[SharedCooldownRegistry] = []


def register_cooldown_registry(registry: SharedCooldownRegistry) -> SharedCooldownRegistry:
    if registry not in _COOLDOWN_REGISTRIES:
        _COOLDOWN_REGISTRIES.append(registry)
    return registry


def cooldown_registries() -> Tuple[SharedCooldownRegistry, ...]:
    return tuple(_COOLDOWN_REGISTRIES)


async def cooldown_sync_loop(interval_seconds: Optional[float] = None) -> None:
    interval = max(0.2, float(interval_seconds or settings.channel_state_sync_interval_seconds or 1.0))
    while True:
        for registry in cooldown_registries():
            try:
                await registry.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("shared cooldown sync failed namespace=%s", registry.namespace)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Cache invalidation bus (pub/sub)
# ---------------------------------------------------------------------------


class CacheInvalidationBus:
    """Broadcasts cache invalidations to every worker via Redis pub/sub.

    Handlers are registered per ``kind`` and receive the invalidated key. A
    worker ignores its own messages (it already applied the change locally).
    ``on_resubscribe`` handlers run after (re)connecting because messages sent
    while disconnected are lost; they should drop the whole local cache.
    """

    def __init__(self, *, guard: Optional[RedisGuard] = None) -> None:
        self._guard = guard or redis_guard
        self._handlers: Dict[str, Callable[[str], Awaitable[None]]] = {}
        self._resubscribe_handlers: List[Callable[[], Awaitable[None]]] = []
        self.connected = False
        self.received = 0
        self.published = 0

    def channel(self) -> str:
        return redis_key("bus", "v1", "invalidate")

    def register(
        self,
        kind: str,
        handler: Callable[[str], Awaitable[None]],
        *,
        on_resubscribe: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._handlers[kind] = handler
        if on_resubscribe is not None and on_resubscribe not in self._resubscribe_handlers:
            self._resubscribe_handlers.append(on_resubscribe)

    async def publish(self, kind: str, key: str) -> bool:
        if not redis_enabled() or not kind:
            return False
        message = json.dumps({"kind": kind, "key": key, "origin": WORKER_ID}, separators=(",", ":"))
        ok, _ = await self._guard.call(
            f"bus.publish.{kind}",
            lambda client: client.publish(self.channel(), message),
        )
        if ok:
            self.published += 1
        return ok

    async def handle_message(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict) or message.get("origin") == WORKER_ID:
            return
        handler = self._handlers.get(str(message.get("kind") or ""))
        if handler is None:
            return
        self.received += 1
        try:
            await handler(str(message.get("key") or ""))
        except Exception:  # noqa: BLE001
            logger.exception("cache invalidation handler failed kind=%s", message.get("kind"))

    async def _on_subscribed(self) -> None:
        for handler in list(self._resubscribe_handlers):
            try:
                await handler()
            except Exception:  # noqa: BLE001
                logger.exception("cache invalidation resubscribe handler failed")

    async def listen_forever(self) -> None:
        backoff = 1.0
        while True:
            pubsub = None
            try:
                client = await get_redis_client()
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(self.channel())
                self.connected = True
                backoff = 1.0
                await self._on_subscribed()
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message and message.get("type") == "message":
                        await self.handle_message(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self.connected:
                    logger.warning("cache invalidation bus disconnected (%s); reconnecting", type(exc).__name__)
                self.connected = False
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                self.connected = False
                if pubsub is not None:
                    try:
                        close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
                        result = close() if close else None
                        if hasattr(result, "__await__"):
                            await result
                    except Exception:  # noqa: BLE001
                        pass


invalidation_bus = CacheInvalidationBus()


# ---------------------------------------------------------------------------
# Leader lock + cluster-wide dedup
# ---------------------------------------------------------------------------

_RENEW_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class LeaderLock:
    """Token-checked lease so a singleton job runs on one worker at a time.

    ``acquire()`` renews the lease when this worker already holds it. When Redis
    is not configured or unavailable it returns ``fail_open`` (default True):
    the jobs guarded by it are idempotent, so running everywhere is the safe
    degradation, matching the pre-Redis behaviour.
    """

    def __init__(self, name: str, ttl_seconds: float, *, fail_open: bool = True, guard: Optional[RedisGuard] = None) -> None:
        self.name = name
        self.ttl_ms = max(1000, int(float(ttl_seconds) * 1000))
        self.fail_open = fail_open
        self._guard = guard or redis_guard
        self._token = f"{WORKER_ID}:{uuid.uuid4().hex}"
        self.is_leader = False

    def key(self) -> str:
        return redis_key("leader", "v1", self.name)

    async def acquire(self) -> bool:
        if not redis_enabled():
            self.is_leader = self.fail_open
            return self.fail_open

        async def _acquire(client: Any) -> bool:
            if self.is_leader:
                renewed = await client.eval(_RENEW_LOCK_SCRIPT, 1, self.key(), self._token, self.ttl_ms)
                if int(renewed or 0) == 1:
                    return True
            acquired = await client.set(self.key(), self._token, nx=True, px=self.ttl_ms)
            return bool(acquired)

        ok, result = await self._guard.call(f"leader.{self.name}", _acquire)
        if not ok:
            self.is_leader = self.fail_open
            return self.fail_open
        if bool(result) and not self.is_leader:
            logger.info("leader lock acquired name=%s worker=%s", self.name, WORKER_ID)
        self.is_leader = bool(result)
        return self.is_leader

    async def release(self) -> None:
        if not self.is_leader or not redis_enabled():
            self.is_leader = False
            return
        await self._guard.call(
            f"leader.{self.name}.release",
            lambda client: client.eval(_RELEASE_LOCK_SCRIPT, 1, self.key(), self._token),
        )
        self.is_leader = False


async def claim_once(scope: str, key: str, ttl_seconds: float, *, fail_open: bool = True) -> bool:
    """Return True for exactly one caller per ``(scope, key)`` within ``ttl_seconds``.

    Falls back to ``fail_open`` when Redis is unavailable (callers already apply
    a process-local dedup first).
    """
    if not redis_enabled() or ttl_seconds <= 0:
        return fail_open
    ttl_ms = max(1, int(float(ttl_seconds) * 1000))
    ok, result = await redis_guard.call(
        f"claim_once.{scope}",
        lambda client: client.set(redis_key("once", "v1", scope, key), WORKER_ID, nx=True, px=ttl_ms),
    )
    if not ok:
        return fail_open
    return bool(result)


def runtime_snapshot() -> Dict[str, Any]:
    """Diagnostics for health/admin endpoints."""
    return {
        "worker_id": WORKER_ID,
        "redis_configured": redis_enabled(),
        "redis_circuit_open": redis_guard.is_open(),
        "invalidation_bus_connected": invalidation_bus.connected,
        "cooldown_registries": [registry.snapshot() for registry in cooldown_registries()],
        "pending_background_tasks": len(_BACKGROUND_TASKS),
    }


def iter_cooldown_namespaces() -> Iterable[str]:
    return (registry.namespace for registry in cooldown_registries())
