from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from .config import settings
from .redis_client import get_redis_client
from .security import generate_id


logger = logging.getLogger("coincoin.usage_events")


@dataclass(frozen=True)
class UsageEvent:
    schema_version: int
    event_id: str
    event_type: str
    status: str
    user_id: str
    api_key_id: str
    request_id: str
    reservation_id: str
    created_at: str
    usage: Dict[str, Any]
    cost: Dict[str, Any]
    request_log: Dict[str, Any]

    def to_stream_fields(self) -> Dict[str, str]:
        return {
            "schema_version": str(self.schema_version),
            "event_id": self.event_id,
            "event_type": self.event_type,
            "status": self.status,
            "user_id": self.user_id,
            "api_key_id": self.api_key_id,
            "request_id": self.request_id,
            "reservation_id": self.reservation_id,
            "created_at": self.created_at,
            "payload": json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, default=str),
        }


def build_usage_event(log: Dict[str, Any]) -> UsageEvent:
    upstream_request_id = str(log.get("upstream_request_id") or "")
    event_id_seed = upstream_request_id or str(log.get("request_id") or "")
    event_id = f"uev_{event_id_seed}" if event_id_seed else generate_id("uev_")
    created_at = log.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_text = created_at.isoformat()
    else:
        created_at_text = datetime.now(timezone.utc).isoformat()

    usage = {
        "unit_type": log.get("usage_unit_type", "tokens"),
        "unit_count": int(log.get("usage_unit_count") or 0),
        "input_tokens": int(log.get("input_tokens") or 0),
        "output_tokens": int(log.get("output_tokens") or 0),
        "cache_read_tokens": int(log.get("cache_read_tokens") or log.get("cached_tokens") or 0),
        "cache_creation_tokens": int(log.get("cache_creation_tokens") or 0),
        "image_count": int(log.get("image_count") or 0),
        "video_count": int(log.get("video_count") or 0),
    }
    cost = {
        "cost_cents": int(log.get("cost_cents") or 0),
        "retail_charge_cents": int(log.get("retail_charge_cents") or log.get("cost_cents") or 0),
        "wholesale_cost_cents": int(log.get("wholesale_cost_cents") or 0),
        "pricing_mode": str(log.get("pricing_mode") or ""),
        "price_version": int(log.get("price_version") or 0),
    }
    return UsageEvent(
        schema_version=1,
        event_id=event_id,
        event_type="usage.recorded",
        status="received",
        user_id=str(log.get("user_id") or ""),
        api_key_id=str(log.get("api_key_id") or ""),
        request_id=upstream_request_id,
        reservation_id=str(log.get("reservation_id") or ""),
        created_at=created_at_text,
        usage=usage,
        cost=cost,
        request_log=dict(log),
    )


class UsageEventPublisher:
    async def publish(self, event: UsageEvent) -> None:
        client = await get_redis_client()
        await asyncio.wait_for(
            client.xadd(settings.usage_event_stream, event.to_stream_fields()),
            timeout=max(0.01, float(settings.usage_event_publish_timeout_seconds or 0.25)),
        )


usage_event_publisher = UsageEventPublisher()


async def publish_usage_event_best_effort(log: Dict[str, Any]) -> None:
    if not settings.usage_event_shadow_enabled:
        return
    try:
        await usage_event_publisher.publish(build_usage_event(log))
    except Exception:
        logger.exception("usage event shadow publish failed")


def schedule_usage_event_shadow(log: Dict[str, Any]) -> None:
    if not settings.usage_event_shadow_enabled:
        return
    try:
        asyncio.create_task(publish_usage_event_best_effort(dict(log)))
    except RuntimeError:
        logger.exception("usage event shadow scheduling failed")
