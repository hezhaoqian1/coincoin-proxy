from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Optional

from .db import SessionLocal
from .models import AlertEvent


logger = logging.getLogger("coincoin.alert_history")


def _short(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


async def create_alert_event(
    *,
    category: str,
    severity: str,
    alert_type: str,
    endpoint: str = "",
    model: str = "",
    channel_id: str = "",
    status_code: int = 0,
    failure_count: int = 0,
    window_seconds: int = 0,
    request_id: str = "",
    destination_type: str = "dingtalk",
    delivery_status: str = "pending",
) -> Optional[str]:
    """Create best-effort delivery evidence without ever blocking delivery on failure."""
    event_id = f"alt_{secrets.token_hex(12)}"
    try:
        async with SessionLocal() as db:
            db.add(
                AlertEvent(
                    id=event_id,
                    category=_short(category, 32),
                    severity=_short(severity, 16),
                    alert_type=_short(alert_type, 64),
                    endpoint=_short(endpoint, 64),
                    model=_short(model, 128),
                    channel_id=_short(channel_id, 32),
                    status_code=max(0, int(status_code or 0)),
                    failure_count=max(0, int(failure_count or 0)),
                    window_seconds=max(0, int(window_seconds or 0)),
                    request_id=_short(request_id, 128),
                    destination_type=_short(destination_type, 32),
                    delivery_status=_short(delivery_status, 16),
                )
            )
            await db.commit()
        return event_id
    except Exception:
        logger.warning("failed to persist pending alert event", exc_info=True)
        return None


async def complete_alert_event(
    event_id: Optional[str],
    *,
    delivery_status: str,
    response_status: int = 0,
    error_summary: str = "",
) -> None:
    """Finish best-effort delivery evidence using only sanitized caller-owned fields."""
    if not event_id:
        return
    try:
        async with SessionLocal() as db:
            event = await db.get(AlertEvent, event_id)
            if event is None:
                return
            event.delivery_status = _short(delivery_status, 16)
            event.response_status = max(0, int(response_status or 0))
            event.error_summary = _short(error_summary, 255)
            event.completed_at = datetime.utcnow()
            await db.commit()
    except Exception:
        logger.warning("failed to complete alert event id=%s", event_id, exc_info=True)
