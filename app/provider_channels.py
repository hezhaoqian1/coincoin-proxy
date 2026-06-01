from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .channel_router import (
    ModelChannelRouteSnapshot,
    ProviderChannelSnapshot,
    channel_router,
)
from .models import ModelChannelRoute, ProviderChannel
from .security import decrypt_api_key


def _timestamp_us(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1_000_000)
    return 0


def _capabilities_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    text = str(raw or "").strip()
    if not text:
        return ()
    return tuple(item.strip() for item in text.replace("\n", ",").split(",") if item.strip())


def _safe_decrypt(encrypted_api_key: str | None) -> str:
    if not encrypted_api_key:
        return ""
    try:
        return decrypt_api_key(encrypted_api_key)
    except Exception:
        return ""


def channel_rows_to_snapshots(rows: Iterable[ProviderChannel]) -> Tuple[list[ProviderChannelSnapshot], int]:
    channels: list[ProviderChannelSnapshot] = []
    version = 0
    for row in rows:
        channel_id = str(getattr(row, "id", "") or "").strip()
        if not channel_id:
            continue
        updated_at = getattr(row, "updated_at", None)
        version = max(version, _timestamp_us(updated_at))
        channels.append(
            ProviderChannelSnapshot(
                channel_id=channel_id,
                name=str(getattr(row, "name", "") or "").strip(),
                provider_platform=str(getattr(row, "provider_platform", "") or "").strip(),
                channel_type=str(getattr(row, "channel_type", "") or "openai_compatible").strip(),
                base_url=str(getattr(row, "base_url", "") or "").strip().rstrip("/"),
                api_key=_safe_decrypt(getattr(row, "encrypted_api_key", None)),
                auth_style=str(getattr(row, "auth_style", "") or "bearer").strip() or "bearer",
                status=str(getattr(row, "status", "") or "active").strip() or "active",
                priority=int(getattr(row, "priority", 0) or 0),
                weight=max(1, int(getattr(row, "weight", 1) or 1)),
                allowed_fails=max(1, int(getattr(row, "allowed_fails", 3) or 3)),
                cooldown_seconds=max(0.0, float(getattr(row, "cooldown_seconds", 30.0) or 0.0)),
                capabilities=_capabilities_tuple(getattr(row, "capabilities", "")),
                provider_account_fingerprint=str(getattr(row, "provider_account_fingerprint", "") or "").strip(),
                cost_tier=str(getattr(row, "cost_tier", "") or "").strip(),
                notes=str(getattr(row, "notes", "") or ""),
                updated_at=updated_at if isinstance(updated_at, datetime) else None,
            )
        )
    return channels, version


def route_rows_to_snapshots(rows: Iterable[ModelChannelRoute]) -> Tuple[list[ModelChannelRouteSnapshot], int]:
    routes: list[ModelChannelRouteSnapshot] = []
    version = 0
    for row in rows:
        route_id = str(getattr(row, "id", "") or "").strip()
        public_model_id = str(getattr(row, "public_model_id", "") or "").strip()
        channel_id = str(getattr(row, "channel_id", "") or "").strip()
        if not (route_id and public_model_id and channel_id):
            continue
        updated_at = getattr(row, "updated_at", None)
        version = max(version, _timestamp_us(updated_at))
        routes.append(
            ModelChannelRouteSnapshot(
                route_id=route_id,
                public_model_id=public_model_id,
                endpoint=str(getattr(row, "endpoint", "") or "").strip(),
                channel_id=channel_id,
                upstream_model=str(getattr(row, "upstream_model", "") or "").strip(),
                priority_override=getattr(row, "priority_override", None),
                weight_override=getattr(row, "weight_override", None),
                transform_profile=str(getattr(row, "transform_profile", "") or "openai_compatible").strip(),
                status=str(getattr(row, "status", "") or "active").strip() or "active",
                notes=str(getattr(row, "notes", "") or ""),
                updated_at=updated_at if isinstance(updated_at, datetime) else None,
            )
        )
    return routes, version


async def load_provider_channel_snapshots_from_db(
    db: AsyncSession,
) -> Tuple[list[ProviderChannelSnapshot], list[ModelChannelRouteSnapshot], int]:
    channel_rows = (await db.execute(select(ProviderChannel))).scalars().all()
    route_rows = (await db.execute(select(ModelChannelRoute))).scalars().all()
    channels, channel_version = channel_rows_to_snapshots(channel_rows)
    routes, route_version = route_rows_to_snapshots(route_rows)
    return channels, routes, max(channel_version, route_version)


async def get_provider_channel_db_state(db: AsyncSession) -> Tuple[int, int, str, str]:
    channel_row = (
        await db.execute(
            select(
                func.count(ProviderChannel.id),
                func.coalesce(func.max(ProviderChannel.updated_at), datetime(1970, 1, 1)),
            )
        )
    ).one()
    route_row = (
        await db.execute(
            select(
                func.count(ModelChannelRoute.id),
                func.coalesce(func.max(ModelChannelRoute.updated_at), datetime(1970, 1, 1)),
            )
        )
    ).one()
    return int(channel_row[0] or 0), int(route_row[0] or 0), str(channel_row[1] or ""), str(route_row[1] or "")


async def refresh_provider_channel_router_from_db(db: AsyncSession) -> None:
    channels, routes, version = await load_provider_channel_snapshots_from_db(db)
    channel_router.set_snapshot(channels, routes, version=version)
