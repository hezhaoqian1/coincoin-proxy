import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Set, Tuple

import httpx

from .config import settings
from .redis_client import get_redis_client

logger = logging.getLogger("coincoin.fallback_alerts")

_ALERT_STATE: Dict[str, float] = {}
_UPSTREAM_FAILURE_BUCKETS: Dict[str, Deque[float]] = {}
_UPSTREAM_FAILURE_DEDUP: Dict[str, float] = {}
_UPSTREAM_FAILURE_LOCK = asyncio.Lock()
_UPSTREAM_FAILURE_TASKS: Set[asyncio.Task] = set()

_REDIS_FAILURE_BURST_SCRIPT = """
local bucket_key = KEYS[1]
local dedup_key = KEYS[2]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local member = ARGV[3]
local threshold = tonumber(ARGV[4])
local dedup_seconds = tonumber(ARGV[5])

redis.call('ZADD', bucket_key, now_ms, member)
redis.call('ZREMRANGEBYSCORE', bucket_key, '-inf', now_ms - window_ms - 1)
redis.call('PEXPIRE', bucket_key, math.max(window_ms * 2, 1000))
local count = redis.call('ZCARD', bucket_key)
local should_send = 0
if count >= threshold then
  local stored = redis.call('SET', dedup_key, '1', 'NX', 'EX', math.max(dedup_seconds, 1))
  if stored then
    should_send = 1
  end
end
return {count, should_send}
"""


def _track_alert_task(task: Any) -> None:
    if not isinstance(task, asyncio.Future):
        return
    _UPSTREAM_FAILURE_TASKS.add(task)
    task.add_done_callback(_finish_upstream_failure_task)


@dataclass(frozen=True)
class FallbackExhaustedAlert:
    endpoint: str
    model: str
    status_code: int
    reason: str
    route_reason: str = ""
    channel_id: str = ""
    fallback_from_channel_id: str = ""
    route_attempt: int = 0
    provider_platform: str = ""
    channel_type: str = ""
    upstream_request_id: str = ""


@dataclass(frozen=True)
class UpstreamFailureBurstAlert:
    endpoint: str
    model: str
    channel_id: str
    status_code: int
    reason: str = ""
    provider_platform: str = ""
    request_id: str = ""


@dataclass(frozen=True)
class UpstreamFailureBurstNotification:
    alert: UpstreamFailureBurstAlert
    category: str
    count: int
    window_seconds: int


def _dedup_key(alert: FallbackExhaustedAlert) -> str:
    return "|".join([
        alert.endpoint or "-",
        alert.model or "-",
        alert.reason or "-",
        str(alert.status_code or 0),
    ])


def _should_send(alert: FallbackExhaustedAlert, now: Optional[float] = None) -> bool:
    webhook_url = (settings.fallback_alert_webhook_url or "").strip()
    if not webhook_url:
        return False
    if int(alert.route_attempt or 0) <= 0 and not (alert.route_reason or "").startswith(("channel_fallback:", "system_fallback:")):
        return False
    now = time.time() if now is None else now
    dedup_seconds = max(0, int(settings.fallback_alert_dedup_seconds or 0))
    key = _dedup_key(alert)
    previous = _ALERT_STATE.get(key, 0.0)
    if dedup_seconds and previous and now - previous < dedup_seconds:
        return False
    _ALERT_STATE[key] = now
    return True


def build_dingtalk_text_payload(alert: FallbackExhaustedAlert) -> Dict[str, Any]:
    title = "CoinCoin fallback 全部失败"
    keyword = (settings.fallback_alert_keyword or "").strip()
    if keyword and keyword not in title:
        title = f"{keyword} {title}"
    lines = [
        title,
        f"模型: {alert.model or '-'}",
        f"Endpoint: {alert.endpoint or '-'}",
        f"状态: HTTP {alert.status_code or 0}",
        f"原因: {alert.reason or '-'}",
        f"最终路由: {alert.route_reason or '-'}",
        f"最终渠道: {alert.channel_id or '-'}",
        f"上一渠道: {alert.fallback_from_channel_id or '-'}",
        f"尝试次数: {int(alert.route_attempt or 0)}",
    ]
    if alert.provider_platform or alert.channel_type:
        lines.append(f"渠道类型: {alert.provider_platform or '-'} / {alert.channel_type or '-'}")
    if alert.upstream_request_id:
        lines.append(f"Upstream request id: {alert.upstream_request_id}")
    lines.append("说明: 真实请求已走完 fallback 链并最终失败；客户端响应照常返回。")
    return {
        "msgtype": "text",
        "text": {"content": "\n".join(lines)},
    }


def _failure_category(alert: UpstreamFailureBurstAlert) -> str:
    if alert.reason == "upstream_unreachable":
        return "availability"
    if int(alert.status_code or 0) == 429:
        return "rate_limit"
    if int(alert.status_code or 0) in {401, 403}:
        return "authentication"
    if int(alert.status_code or 0) >= 500:
        return "availability"
    return ""


def _failure_category_label(category: str) -> str:
    return {
        "availability": "可用性错误",
        "rate_limit": "429 限流",
        "authentication": "401/403 鉴权错误",
    }.get(category, "上游错误")


def build_upstream_failure_burst_payload(notification: UpstreamFailureBurstNotification) -> Dict[str, Any]:
    alert = notification.alert
    category_label = _failure_category_label(notification.category)
    title = f"CoinCoin 上游{category_label}告警"
    keyword = (settings.fallback_alert_keyword or "").strip()
    if keyword and keyword not in title:
        title = f"{keyword} {title}"
    lines = [
        title,
        f"真实用户请求在 {notification.window_seconds} 秒内累计 {notification.count} 次{category_label}",
        f"Endpoint: {alert.endpoint or '-'}",
        f"模型: {alert.model or '-'}",
        f"渠道: {alert.channel_id or '-'}",
        f"最近状态: HTTP {int(alert.status_code or 0)}",
    ]
    if alert.reason:
        lines.append(f"最近原因: {alert.reason}")
    if alert.provider_platform:
        lines.append(f"渠道平台: {alert.provider_platform}")
    if alert.request_id:
        lines.append(f"最近 Request ID: {alert.request_id}")
    lines.append("说明: 只统计经过真实用户 /messages 调用路径的上游失败；健康检查、监控探针和后台测试不计入。")
    return {"msgtype": "text", "text": {"content": "\n".join(lines)}}


async def _send_dingtalk_alert(alert: FallbackExhaustedAlert) -> None:
    webhook_url = (settings.fallback_alert_webhook_url or "").strip()
    if not webhook_url:
        return
    payload = build_dingtalk_text_payload(alert)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(webhook_url, json=payload)
        if response.status_code >= 400:
            logger.warning("dingtalk fallback alert failed status=%s body=%s", response.status_code, response.text[:300])
    except Exception:
        logger.warning("dingtalk fallback alert send failed", exc_info=True)


async def _send_upstream_failure_burst_alert(notification: UpstreamFailureBurstNotification) -> None:
    webhook_url = (settings.fallback_alert_webhook_url or "").strip()
    if not webhook_url:
        return
    payload = build_upstream_failure_burst_payload(notification)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(webhook_url, json=payload)
        if response.status_code >= 400:
            logger.warning("dingtalk upstream failure alert failed status=%s body=%s", response.status_code, response.text[:300])
    except Exception:
        logger.warning("dingtalk upstream failure alert send failed", exc_info=True)


def notify_fallback_exhausted(alert: FallbackExhaustedAlert) -> bool:
    if not _should_send(alert):
        return False
    try:
        task = asyncio.create_task(_send_dingtalk_alert(alert))
        _track_alert_task(task)
        return True
    except RuntimeError:
        logger.warning("no running event loop for fallback alert")
        return False


def notify_upstream_failure_burst(notification: UpstreamFailureBurstNotification) -> bool:
    try:
        task = asyncio.create_task(_send_upstream_failure_burst_alert(notification))
        _track_alert_task(task)
        return True
    except RuntimeError:
        logger.warning("no running event loop for upstream failure alert")
        return False


def _upstream_failure_key(alert: UpstreamFailureBurstAlert, category: str) -> str:
    return f"{category}|{alert.channel_id or '-'}|{alert.endpoint or '-'}"


def _record_upstream_failure_local(
    alert: UpstreamFailureBurstAlert,
    category: str,
    *,
    now: float,
    threshold: int,
    window_seconds: int,
    dedup_seconds: int,
) -> Tuple[int, bool]:
    key = _upstream_failure_key(alert, category)
    bucket = _UPSTREAM_FAILURE_BUCKETS.setdefault(key, deque())
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    bucket.append(now)
    count = len(bucket)
    previous = _UPSTREAM_FAILURE_DEDUP.get(key, 0.0)
    if count < threshold or (previous and now - previous < dedup_seconds):
        return count, False
    _UPSTREAM_FAILURE_DEDUP[key] = now
    return count, True


async def _record_upstream_failure_redis(
    alert: UpstreamFailureBurstAlert,
    category: str,
    *,
    now: float,
    threshold: int,
    window_seconds: int,
    dedup_seconds: int,
) -> Tuple[int, bool]:
    client = await get_redis_client()
    digest = hashlib.sha256(_upstream_failure_key(alert, category).encode("utf-8")).hexdigest()[:24]
    prefix = (settings.redis_key_prefix or "coincoin").strip() or "coincoin"
    bucket_key = f"{prefix}:alerts:upstream-failure:v1:{digest}"
    dedup_key = f"{bucket_key}:dedup"
    now_ms = int(now * 1000)
    member = f"{now_ms}:{alert.request_id or time.monotonic_ns()}"
    result = await client.eval(
        _REDIS_FAILURE_BURST_SCRIPT,
        2,
        bucket_key,
        dedup_key,
        now_ms,
        int(window_seconds * 1000),
        member,
        threshold,
        dedup_seconds,
    )
    count = int(result[0] or 0)
    should_send = bool(int(result[1] or 0))
    return count, should_send


async def record_user_upstream_failure(alert: UpstreamFailureBurstAlert, *, now: Optional[float] = None) -> bool:
    if not (settings.fallback_alert_webhook_url or "").strip():
        return False
    category = _failure_category(alert)
    if not category:
        return False
    configured_threshold = (
        settings.upstream_auth_alert_threshold
        if category == "authentication"
        else settings.upstream_failure_alert_threshold
    )
    threshold = max(1, int(configured_threshold or 1))
    window_seconds = max(1, int(settings.upstream_failure_alert_window_seconds or 60))
    dedup_seconds = max(window_seconds, int(settings.upstream_failure_alert_dedup_seconds or 300))
    current_time = time.time() if now is None else float(now)

    count = 0
    should_send = False
    if settings.redis_url and now is None:
        try:
            count, should_send = await asyncio.wait_for(
                _record_upstream_failure_redis(
                    alert,
                    category,
                    now=current_time,
                    threshold=threshold,
                    window_seconds=window_seconds,
                    dedup_seconds=dedup_seconds,
                ),
                timeout=0.25,
            )
        except Exception:
            logger.warning("redis upstream failure alert counter failed; using local fallback", exc_info=True)
    if not settings.redis_url or now is not None or (count == 0 and not should_send):
        async with _UPSTREAM_FAILURE_LOCK:
            count, should_send = _record_upstream_failure_local(
                alert,
                category,
                now=current_time,
                threshold=threshold,
                window_seconds=window_seconds,
                dedup_seconds=dedup_seconds,
            )

    if not should_send:
        return False
    notification = UpstreamFailureBurstNotification(
        alert=alert,
        category=category,
        count=count,
        window_seconds=window_seconds,
    )
    notify_upstream_failure_burst(notification)
    return True


def _finish_upstream_failure_task(task: asyncio.Task) -> None:
    _UPSTREAM_FAILURE_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.warning("background upstream failure alert tracking failed", exc_info=True)


def schedule_user_upstream_failure(alert: UpstreamFailureBurstAlert) -> bool:
    """Track alert bursts without delaying the channel fallback request path."""
    if not (settings.fallback_alert_webhook_url or "").strip() or not _failure_category(alert):
        return False
    max_pending = max(1, int(settings.fallback_alert_max_pending_tasks or 256))
    if len(_UPSTREAM_FAILURE_TASKS) >= max_pending:
        logger.warning("upstream failure alert tracking queue full pending=%s", len(_UPSTREAM_FAILURE_TASKS))
        return False
    try:
        task = asyncio.create_task(record_user_upstream_failure(alert))
    except RuntimeError:
        logger.warning("no running event loop for upstream failure alert tracking")
        return False
    _track_alert_task(task)
    return True


async def shutdown_fallback_alerts(*, timeout_seconds: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_seconds))
    while True:
        tasks = [task for task in _UPSTREAM_FAILURE_TASKS if not task.done()]
        if not tasks:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return
        _, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return


def reset_fallback_alert_state() -> None:
    _ALERT_STATE.clear()
    _UPSTREAM_FAILURE_BUCKETS.clear()
    _UPSTREAM_FAILURE_DEDUP.clear()
    for task in list(_UPSTREAM_FAILURE_TASKS):
        if isinstance(task, asyncio.Future) and not task.done():
            task.cancel()
    _UPSTREAM_FAILURE_TASKS.clear()
