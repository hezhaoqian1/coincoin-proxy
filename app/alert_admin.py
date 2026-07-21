from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .admin import admin_guard
from .config import settings
from .db import get_db
from .fallback_alerts import (
    current_alert_policy,
    send_dingtalk_configuration_test,
    set_runtime_alert_settings,
)
from .models import AlertEvent, SystemSetting


router = APIRouter(
    prefix="/admin/alerts",
    tags=["admin-alerts"],
    dependencies=[Depends(admin_guard)],
)

POLICY_SETTING_KEYS = {
    "enabled": "fallback_alert_enabled",
    "availability_threshold": "upstream_failure_alert_threshold",
    "authentication_threshold": "upstream_auth_alert_threshold",
    "window_seconds": "upstream_failure_alert_window_seconds",
    "dedup_seconds": "upstream_failure_alert_dedup_seconds",
    "max_pending_tasks": "fallback_alert_max_pending_tasks",
}


class AlertPolicyUpdate(BaseModel):
    enabled: bool
    availability_threshold: int = Field(ge=1, le=1000)
    authentication_threshold: int = Field(ge=1, le=1000)
    window_seconds: int = Field(ge=1, le=3600)
    dedup_seconds: int = Field(ge=1, le=86400)
    max_pending_tasks: int = Field(ge=1, le=4096)

    @model_validator(mode="after")
    def validate_dedup_window(self) -> "AlertPolicyUpdate":
        if self.dedup_seconds < self.window_seconds:
            raise ValueError("dedup_seconds must be greater than or equal to window_seconds")
        return self


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


async def _latest_delivery_times(db: AsyncSession) -> dict[str, Optional[str]]:
    result = await db.execute(
        select(AlertEvent.delivery_status, func.max(AlertEvent.completed_at))
        .where(AlertEvent.delivery_status.in_(("sent", "failed")))
        .group_by(AlertEvent.delivery_status)
    )
    latest = {str(delivery_status): completed_at for delivery_status, completed_at in result.all()}
    return {
        "last_success_at": _iso(latest.get("sent")),
        "last_failure_at": _iso(latest.get("failed")),
    }


async def _config_payload(db: AsyncSession) -> dict[str, Any]:
    policy = current_alert_policy()
    return {
        "enabled": policy.enabled,
        "webhook_configured": bool((settings.fallback_alert_webhook_url or "").strip()),
        "availability_threshold": policy.availability_threshold,
        "authentication_threshold": policy.authentication_threshold,
        "window_seconds": policy.window_seconds,
        "dedup_seconds": policy.dedup_seconds,
        "max_pending_tasks": policy.max_pending_tasks,
        **(await _latest_delivery_times(db)),
    }


@router.get("/config")
async def get_alert_config(db: AsyncSession = Depends(get_db)):
    return await _config_payload(db)


@router.patch("/config")
async def update_alert_config(payload: AlertPolicyUpdate, db: AsyncSession = Depends(get_db)):
    values = {
        POLICY_SETTING_KEYS["enabled"]: "true" if payload.enabled else "false",
        POLICY_SETTING_KEYS["availability_threshold"]: str(payload.availability_threshold),
        POLICY_SETTING_KEYS["authentication_threshold"]: str(payload.authentication_threshold),
        POLICY_SETTING_KEYS["window_seconds"]: str(payload.window_seconds),
        POLICY_SETTING_KEYS["dedup_seconds"]: str(payload.dedup_seconds),
        POLICY_SETTING_KEYS["max_pending_tasks"]: str(payload.max_pending_tasks),
    }
    for setting_key, setting_value in values.items():
        row = await db.get(SystemSetting, setting_key)
        if row is None:
            row = SystemSetting(setting_key=setting_key, setting_value=setting_value)
            db.add(row)
        row.setting_value = setting_value
        row.updated_by = "admin"
    await db.commit()
    set_runtime_alert_settings(values)
    return await _config_payload(db)


@router.post("/test")
async def test_alert_destination():
    if not (settings.fallback_alert_webhook_url or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="DingTalk alert webhook is not configured",
        )
    return await send_dingtalk_configuration_test()


@router.get("/events")
async def list_alert_events(
    category: Optional[
        Literal[
            "availability",
            "rate_limit",
            "authentication",
            "fallback_exhausted",
            "configuration_test",
        ]
    ] = None,
    delivery_status: Optional[Literal["pending", "sent", "failed"]] = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    statement = select(AlertEvent)
    if category:
        statement = statement.where(AlertEvent.category == category)
    if delivery_status:
        statement = statement.where(AlertEvent.delivery_status == delivery_status)
    result = await db.execute(statement.order_by(AlertEvent.created_at.desc()).limit(limit))
    events = result.scalars().all()
    return {
        "events": [
            {
                "id": event.id,
                "category": event.category,
                "severity": event.severity,
                "alert_type": event.alert_type,
                "endpoint": event.endpoint,
                "model": event.model,
                "channel_id": event.channel_id,
                "status_code": int(event.status_code or 0),
                "failure_count": int(event.failure_count or 0),
                "window_seconds": int(event.window_seconds or 0),
                "request_id": event.request_id,
                "destination_type": event.destination_type,
                "delivery_status": event.delivery_status,
                "response_status": int(event.response_status or 0),
                "error_summary": event.error_summary,
                "created_at": _iso(event.created_at),
                "completed_at": _iso(event.completed_at),
            }
            for event in events
        ],
        "limit": limit,
    }
