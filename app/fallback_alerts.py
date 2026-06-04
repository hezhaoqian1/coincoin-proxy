import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from .config import settings

logger = logging.getLogger("coincoin.fallback_alerts")

_ALERT_STATE: Dict[str, float] = {}


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


def notify_fallback_exhausted(alert: FallbackExhaustedAlert) -> bool:
    if not _should_send(alert):
        return False
    try:
        asyncio.create_task(_send_dingtalk_alert(alert))
        return True
    except RuntimeError:
        logger.warning("no running event loop for fallback alert")
        return False


def reset_fallback_alert_state() -> None:
    _ALERT_STATE.clear()
