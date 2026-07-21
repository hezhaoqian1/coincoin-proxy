import asyncio
import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote_plus, urlsplit

import httpx

from .alert_history import complete_alert_event, create_alert_event
from .config import settings
from .redis_client import get_redis_client

logger = logging.getLogger("coincoin.fallback_alerts")

_ALERT_STATE: Dict[str, float] = {}
_UPSTREAM_FAILURE_BUCKETS: Dict[str, Deque[float]] = {}
_UPSTREAM_FAILURE_DEDUP: Dict[str, float] = {}
_UPSTREAM_FAILURE_LOCK = asyncio.Lock()
_UPSTREAM_FAILURE_TASKS: Set[asyncio.Task] = set()
_RUNTIME_ALERT_SETTINGS: Dict[str, str] = {}
_ALERT_HISTORY_TIMEOUT_SECONDS = 0.25
_DINGTALK_LOG_URL_PATTERN = re.compile(
    r"https://oapi\.dingtalk\.com/robot/send\?[^\s\"]+",
    re.IGNORECASE,
)

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


def _alert_task_capacity_available(max_pending: Optional[int] = None) -> bool:
    limit = max_pending if max_pending is not None else current_alert_policy().max_pending_tasks
    if len(_UPSTREAM_FAILURE_TASKS) < max(1, int(limit or 1)):
        return True
    logger.warning("alert background task queue full pending=%s", len(_UPSTREAM_FAILURE_TASKS))
    return False


async def _create_alert_event_bounded(**fields: Any) -> Optional[str]:
    try:
        return await asyncio.wait_for(
            create_alert_event(**fields),
            timeout=_ALERT_HISTORY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("pending alert event persistence timed out")
    except Exception as exc:
        logger.warning("pending alert event persistence failed error=%s", type(exc).__name__)
    return None


async def _complete_alert_event_bounded(event_id: Optional[str], **fields: Any) -> None:
    if not event_id:
        return
    try:
        await asyncio.wait_for(
            complete_alert_event(event_id, **fields),
            timeout=_ALERT_HISTORY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("alert event completion persistence timed out id=%s", event_id)
    except Exception as exc:
        logger.warning("alert event completion persistence failed id=%s error=%s", event_id, type(exc).__name__)


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


@dataclass(frozen=True)
class AlertPolicy:
    enabled: bool
    availability_threshold: int
    authentication_threshold: int
    window_seconds: int
    dedup_seconds: int
    max_pending_tasks: int


def _bool_setting(value: Any, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_setting(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_RUNTIME_ALERT_SETTINGS.get(key, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def set_runtime_alert_settings(runtime_settings: Dict[str, Any]) -> None:
    keys = {
        "fallback_alert_webhook_url",
        "fallback_alert_enabled",
        "upstream_failure_alert_threshold",
        "upstream_auth_alert_threshold",
        "upstream_failure_alert_window_seconds",
        "upstream_failure_alert_dedup_seconds",
        "fallback_alert_max_pending_tasks",
    }
    _RUNTIME_ALERT_SETTINGS.clear()
    _RUNTIME_ALERT_SETTINGS.update(
        {
            key: str(value or "").strip()
            for key, value in (runtime_settings or {}).items()
            if key in keys
        }
    )


def current_alert_webhook_url() -> str:
    if "fallback_alert_webhook_url" in _RUNTIME_ALERT_SETTINGS:
        return _RUNTIME_ALERT_SETTINGS["fallback_alert_webhook_url"]
    return str(settings.fallback_alert_webhook_url or "").strip()


def is_valid_dingtalk_webhook_url(value: str) -> bool:
    if not value or any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        access_tokens = parse_qs(parsed.query, keep_blank_values=True).get(
            "access_token", []
        )
    except ValueError:
        return False
    return not (
        value != value.strip()
        or parsed.scheme != "https"
        or parsed.netloc != "oapi.dingtalk.com"
        or parsed.path != "/robot/send"
        or len(access_tokens) != 1
        or not access_tokens[0]
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or character.isspace()
            for character in access_tokens[0]
        )
    )


def current_sendable_alert_webhook_url() -> str:
    webhook_url = current_alert_webhook_url()
    return webhook_url if is_valid_dingtalk_webhook_url(webhook_url) else ""


def current_alert_policy() -> AlertPolicy:
    window_seconds = _int_setting(
        "upstream_failure_alert_window_seconds",
        int(settings.upstream_failure_alert_window_seconds or 60),
        1,
        3600,
    )
    dedup_seconds = _int_setting(
        "upstream_failure_alert_dedup_seconds",
        int(settings.upstream_failure_alert_dedup_seconds or 300),
        1,
        86400,
    )
    return AlertPolicy(
        enabled=_bool_setting(
            _RUNTIME_ALERT_SETTINGS.get("fallback_alert_enabled"),
            bool(settings.fallback_alert_enabled),
        ),
        availability_threshold=_int_setting(
            "upstream_failure_alert_threshold",
            int(settings.upstream_failure_alert_threshold or 5),
            1,
            1000,
        ),
        authentication_threshold=_int_setting(
            "upstream_auth_alert_threshold",
            int(settings.upstream_auth_alert_threshold or 3),
            1,
            1000,
        ),
        window_seconds=window_seconds,
        dedup_seconds=max(window_seconds, dedup_seconds),
        max_pending_tasks=_int_setting(
            "fallback_alert_max_pending_tasks",
            int(settings.fallback_alert_max_pending_tasks or 256),
            1,
            4096,
        ),
    )


def _dedup_key(alert: FallbackExhaustedAlert) -> str:
    return "|".join([
        alert.endpoint or "-",
        alert.model or "-",
        alert.reason or "-",
        str(alert.status_code or 0),
    ])


def _should_send(alert: FallbackExhaustedAlert, now: Optional[float] = None) -> bool:
    if not current_alert_policy().enabled:
        return False
    webhook_url = current_sendable_alert_webhook_url()
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


def build_configuration_test_payload() -> Dict[str, Any]:
    title = "CoinCoin 告警配置测试"
    keyword = (settings.fallback_alert_keyword or "").strip()
    if keyword and keyword not in title:
        title = f"{keyword} {title}"
    return {
        "msgtype": "text",
        "text": {
            "content": "\n".join(
                [
                    title,
                    "配置测试",
                    "说明: 这是管理员主动发起的钉钉告警配置测试，不代表线上请求发生故障。",
                ]
            )
        },
    }


def _dingtalk_delivery_result(response: Any) -> Tuple[bool, str]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        return False, f"DingTalk HTTP {status_code}"
    try:
        payload = response.json()
    except Exception:
        return False, "DingTalk invalid response"
    if not isinstance(payload, dict) or "errcode" not in payload:
        return False, "DingTalk invalid response"
    errcode = payload.get("errcode")
    if errcode not in (0, "0"):
        return False, f"DingTalk errcode {str(errcode)[:32]}"
    return True, ""


def _redact_dingtalk_access_token(value: str) -> str:
    def redact_url(match: re.Match[str]) -> str:
        url = match.group(0)
        query_start = url.index("?") + 1
        fragment_start = url.find("#", query_start)
        query_end = fragment_start if fragment_start >= 0 else len(url)
        query_parts = url[query_start:query_end].split("&")
        for index, query_part in enumerate(query_parts):
            name, separator, _ = query_part.partition("=")
            if separator and unquote_plus(name) == "access_token":
                query_parts[index] = f"{name}=[REDACTED]"
        return "".join(
            (
                url[:query_start],
                "&".join(query_parts),
                url[query_end:],
            )
        )

    return _DINGTALK_LOG_URL_PATTERN.sub(redact_url, value)


class _DingTalkAccessTokenLogFilter(logging.Filter):
    _coincoin_dingtalk_token_filter = True

    @staticmethod
    def _redact_value(value: Any) -> Any:
        rendered = str(value)
        redacted = _redact_dingtalk_access_token(rendered)
        return redacted if redacted != rendered else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_dingtalk_access_token(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact_value(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: self._redact_value(value) for key, value in record.args.items()
            }
        return True


_HTTPX_DINGTALK_TOKEN_FILTER = _DingTalkAccessTokenLogFilter()


def _install_httpx_dingtalk_token_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(
        getattr(installed_filter, "_coincoin_dingtalk_token_filter", False)
        for installed_filter in httpx_logger.filters
    ):
        httpx_logger.addFilter(_HTTPX_DINGTALK_TOKEN_FILTER)


def _dingtalk_http_client() -> httpx.AsyncClient:
    _install_httpx_dingtalk_token_filter()
    return httpx.AsyncClient(timeout=5.0)


async def _send_dingtalk_alert(alert: FallbackExhaustedAlert) -> bool:
    webhook_url = current_sendable_alert_webhook_url()
    if not webhook_url:
        return False
    payload = build_dingtalk_text_payload(alert)
    event_id = await _create_alert_event_bounded(
        category="fallback_exhausted",
        severity="critical",
        alert_type="fallback_exhausted",
        endpoint=alert.endpoint,
        model=alert.model,
        channel_id=alert.channel_id,
        status_code=alert.status_code,
        request_id=alert.upstream_request_id,
        delivery_status="pending",
    )
    try:
        async with _dingtalk_http_client() as client:
            response = await client.post(webhook_url, json=payload)
        delivered, error_summary = _dingtalk_delivery_result(response)
        await _complete_alert_event_bounded(
            event_id,
            delivery_status="sent" if delivered else "failed",
            response_status=int(response.status_code or 0),
            error_summary=error_summary,
        )
        if not delivered:
            logger.warning("dingtalk fallback alert failed status=%s", response.status_code)
        return delivered
    except Exception as exc:
        await _complete_alert_event_bounded(
            event_id,
            delivery_status="failed",
            response_status=0,
            error_summary=f"DingTalk delivery {type(exc).__name__}",
        )
        logger.warning("dingtalk fallback alert send failed error=%s", type(exc).__name__)
        return False


async def _send_upstream_failure_burst_alert(notification: UpstreamFailureBurstNotification) -> bool:
    webhook_url = current_sendable_alert_webhook_url()
    if not webhook_url:
        return False
    payload = build_upstream_failure_burst_payload(notification)
    alert = notification.alert
    event_id = await _create_alert_event_bounded(
        category=notification.category,
        severity="warning" if notification.category == "rate_limit" else "critical",
        alert_type="upstream_failure_burst",
        endpoint=alert.endpoint,
        model=alert.model,
        channel_id=alert.channel_id,
        status_code=alert.status_code,
        failure_count=notification.count,
        window_seconds=notification.window_seconds,
        request_id=alert.request_id,
        delivery_status="pending",
    )
    try:
        async with _dingtalk_http_client() as client:
            response = await client.post(webhook_url, json=payload)
        delivered, error_summary = _dingtalk_delivery_result(response)
        await _complete_alert_event_bounded(
            event_id,
            delivery_status="sent" if delivered else "failed",
            response_status=int(response.status_code or 0),
            error_summary=error_summary,
        )
        if not delivered:
            logger.warning("dingtalk upstream failure alert failed status=%s", response.status_code)
        return delivered
    except Exception as exc:
        await _complete_alert_event_bounded(
            event_id,
            delivery_status="failed",
            response_status=0,
            error_summary=f"DingTalk delivery {type(exc).__name__}",
        )
        logger.warning("dingtalk upstream failure alert send failed error=%s", type(exc).__name__)
        return False


async def send_dingtalk_configuration_test() -> Dict[str, Any]:
    webhook_url = current_sendable_alert_webhook_url()
    if not webhook_url:
        return {"sent": False, "event_id": None}
    event_id = await _create_alert_event_bounded(
        category="configuration_test",
        severity="info",
        alert_type="configuration_test",
        destination_type="dingtalk",
        delivery_status="pending",
    )
    try:
        async with _dingtalk_http_client() as client:
            response = await client.post(webhook_url, json=build_configuration_test_payload())
        delivered, error_summary = _dingtalk_delivery_result(response)
        await _complete_alert_event_bounded(
            event_id,
            delivery_status="sent" if delivered else "failed",
            response_status=int(response.status_code or 0),
            error_summary=error_summary,
        )
        if not delivered:
            logger.warning("dingtalk configuration test failed status=%s", response.status_code)
        return {"sent": delivered, "event_id": event_id}
    except Exception as exc:
        await _complete_alert_event_bounded(
            event_id,
            delivery_status="failed",
            response_status=0,
            error_summary=f"DingTalk delivery {type(exc).__name__}",
        )
        logger.warning("dingtalk configuration test send failed error=%s", type(exc).__name__)
        return {"sent": False, "event_id": event_id}


def notify_fallback_exhausted(alert: FallbackExhaustedAlert) -> bool:
    if not _alert_task_capacity_available():
        return False
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
    if not _alert_task_capacity_available():
        return False
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
    policy = current_alert_policy()
    if not policy.enabled or not current_sendable_alert_webhook_url():
        return False
    category = _failure_category(alert)
    if not category:
        return False
    configured_threshold = (
        policy.authentication_threshold
        if category == "authentication"
        else policy.availability_threshold
    )
    threshold = max(1, int(configured_threshold or 1))
    window_seconds = policy.window_seconds
    dedup_seconds = policy.dedup_seconds
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
    await _send_upstream_failure_burst_alert(notification)
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
    policy = current_alert_policy()
    if not policy.enabled or not current_sendable_alert_webhook_url() or not _failure_category(alert):
        return False
    max_pending = policy.max_pending_tasks
    if not _alert_task_capacity_available(max_pending):
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
    _RUNTIME_ALERT_SETTINGS.clear()
    for task in list(_UPSTREAM_FAILURE_TASKS):
        if isinstance(task, asyncio.Future) and not task.done():
            task.cancel()
    _UPSTREAM_FAILURE_TASKS.clear()
