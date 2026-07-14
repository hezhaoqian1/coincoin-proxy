from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .channel_monitoring import AUTO_MONITOR_CREATED_BY, monitor_model_list
from .db import get_db
from .models import (
    ModelChannelRoute,
    ProviderChannel,
    ProviderChannelMonitor,
    ProviderChannelRuntimeState,
    RequestLog,
)
from .security import require_admin


router = APIRouter(prefix="/admin/reliability", tags=["admin-reliability"])

RELIABILITY_CACHE_TTL_SECONDS = 10
_CACHE_VALUE: dict[str, Any] | None = None
_CACHE_EXPIRES_AT = 0.0
_CACHE_LOCK = asyncio.Lock()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.isoformat()


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    value = getattr(row, name, None)
    if value is not None:
        return value
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(name, default)
    return default


def _status_rank(status: str) -> int:
    return {
        "failed": 6,
        "cooling": 5,
        "degraded": 4,
        "pending": 3,
        "unconfigured": 2,
        "disabled": 1,
        "operational": 0,
    }.get(str(status or "").strip().lower(), 3)


def _worst_status(statuses: Iterable[str], *, default: str = "operational") -> str:
    values = [str(item or "").strip().lower() for item in statuses if str(item or "").strip()]
    return max(values, key=_status_rank) if values else default


def _runtime_cooling(runtime_state: Any, now: datetime) -> bool:
    cooldown_until = getattr(runtime_state, "cooldown_until", None)
    if not isinstance(cooldown_until, datetime):
        return False
    if cooldown_until.tzinfo is not None:
        cooldown_until = cooldown_until.astimezone(UTC).replace(tzinfo=None)
    return cooldown_until > now


def _traffic_payload(row: Any) -> dict[str, Any]:
    requests = max(0, int(_row_value(row, "requests", 0) or 0))
    fallback_requests = max(0, int(_row_value(row, "fallback_requests", 0) or 0))
    return {
        "requests": requests,
        "success_requests": max(0, int(_row_value(row, "success_requests", 0) or 0)),
        "failed_requests": max(0, int(_row_value(row, "failed_requests", 0) or 0)),
        "fallback_requests": fallback_requests,
        "fallback_rate": round(fallback_requests / requests, 4) if requests else 0.0,
        "avg_latency_ms": max(0, round(float(_row_value(row, "avg_latency_ms", 0) or 0))),
        "max_latency_ms": max(0, int(_row_value(row, "max_latency_ms", 0) or 0)),
        "last_seen_at": _as_iso(_row_value(row, "last_seen_at")),
    }


def _empty_traffic() -> dict[str, Any]:
    return _traffic_payload(SimpleTrafficRow())


def _merge_traffic(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return dict(incoming)
    requests = int(current["requests"]) + int(incoming["requests"])
    weighted_latency = (
        int(current["avg_latency_ms"]) * int(current["requests"])
        + int(incoming["avg_latency_ms"]) * int(incoming["requests"])
    )
    fallback_requests = int(current["fallback_requests"]) + int(incoming["fallback_requests"])
    last_seen_values = [value for value in (current.get("last_seen_at"), incoming.get("last_seen_at")) if value]
    return {
        "requests": requests,
        "success_requests": int(current["success_requests"]) + int(incoming["success_requests"]),
        "failed_requests": int(current["failed_requests"]) + int(incoming["failed_requests"]),
        "fallback_requests": fallback_requests,
        "fallback_rate": round(fallback_requests / requests, 4) if requests else 0.0,
        "avg_latency_ms": round(weighted_latency / requests) if requests else 0,
        "max_latency_ms": max(int(current["max_latency_ms"]), int(incoming["max_latency_ms"])),
        "last_seen_at": max(last_seen_values) if last_seen_values else None,
    }


def _monitor_status(monitor: Any) -> str:
    status = str(getattr(monitor, "last_status", "") or "").strip().lower()
    if status in {"failed", "error"}:
        return "failed"
    if status == "degraded":
        return "degraded"
    if status == "operational":
        return "operational"
    return "pending"


def assemble_reliability_overview(
    *,
    channels: Iterable[Any],
    routes: Iterable[Any],
    runtime_states: Iterable[Any],
    monitors: Iterable[Any],
    traffic_rows: Iterable[Any],
    recent_failures: Iterable[Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _utcnow()
    channel_list = list(channels)
    route_list = list(routes)
    monitor_list = list(monitors)
    runtime_by_channel = {str(row.channel_id): row for row in runtime_states}
    channel_by_id = {str(row.id): row for row in channel_list}

    routes_by_channel: dict[str, list[Any]] = {}
    routes_by_model: dict[str, list[Any]] = {}
    for route in route_list:
        routes_by_channel.setdefault(str(route.channel_id), []).append(route)
        routes_by_model.setdefault(str(route.public_model_id), []).append(route)

    monitors_by_channel: dict[str, list[Any]] = {}
    for monitor in monitor_list:
        monitors_by_channel.setdefault(str(monitor.channel_id), []).append(monitor)

    traffic_by_channel: dict[str, dict[str, Any]] = {}
    traffic_by_model: dict[str, dict[str, Any]] = {}
    traffic_by_model_channel: dict[tuple[str, str], dict[str, Any]] = {}
    for row in traffic_rows:
        channel_id = str(_row_value(row, "channel_id", "") or "")
        public_model_id = str(_row_value(row, "public_model_id", "") or "")
        traffic = _traffic_payload(row)
        traffic_by_channel[channel_id] = _merge_traffic(traffic_by_channel.get(channel_id), traffic)
        traffic_by_model[public_model_id] = _merge_traffic(traffic_by_model.get(public_model_id), traffic)
        traffic_key = (public_model_id, channel_id)
        traffic_by_model_channel[traffic_key] = _merge_traffic(traffic_by_model_channel.get(traffic_key), traffic)

    channel_payloads: list[dict[str, Any]] = []
    for channel in sorted(channel_list, key=lambda item: (int(getattr(item, "priority", 0) or 0), str(item.name or ""))):
        channel_id = str(channel.id)
        channel_routes = routes_by_channel.get(channel_id, [])
        active_routes = [row for row in channel_routes if str(getattr(row, "status", "") or "").lower() == "active"]
        channel_monitors = [
            row for row in monitors_by_channel.get(channel_id, [])
            if str(getattr(row, "status", "") or "").lower() == "active"
        ]
        traffic = traffic_by_channel.get(channel_id, _empty_traffic())
        runtime_state = runtime_by_channel.get(channel_id)
        configured_status = str(getattr(channel, "status", "") or "").strip().lower()
        monitor_status = _worst_status((_monitor_status(row) for row in channel_monitors), default="pending")

        if configured_status != "active":
            health_status = "disabled"
        elif not active_routes:
            health_status = "unconfigured"
        elif _runtime_cooling(runtime_state, now):
            health_status = "cooling"
        elif monitor_status == "failed":
            health_status = "failed"
        elif monitor_status == "degraded" or traffic["failed_requests"] > 0 or traffic["fallback_rate"] >= 0.05:
            health_status = "degraded"
        elif monitor_status == "pending" and traffic["requests"] == 0:
            health_status = "pending"
        else:
            health_status = "operational"

        primary_monitor = max(
            channel_monitors,
            key=lambda row: _status_rank(_monitor_status(row)),
            default=None,
        )
        monitor_models = monitor_model_list(primary_monitor) if primary_monitor is not None else []
        monitor_mode = ""
        if primary_monitor is not None:
            monitor_mode = (
                "auto"
                if str(getattr(primary_monitor, "created_by", "") or "") == AUTO_MONITOR_CREATED_BY
                else "manual"
            )
        channel_payloads.append(
            {
                "id": channel_id,
                "name": str(getattr(channel, "name", "") or channel_id),
                "provider_platform": str(getattr(channel, "provider_platform", "") or ""),
                "channel_type": str(getattr(channel, "channel_type", "") or ""),
                "configured_status": configured_status or "disabled",
                "health_status": health_status,
                "priority": int(getattr(channel, "priority", 0) or 0),
                "weight": max(1, int(getattr(channel, "weight", 1) or 1)),
                "route_count": len(channel_routes),
                "active_route_count": len(active_routes),
                "public_models": sorted({str(row.public_model_id) for row in active_routes}),
                "monitor_id": str(getattr(primary_monitor, "id", "") or ""),
                "monitor_status": monitor_status,
                "monitor_message": str(getattr(primary_monitor, "last_message", "") or "")[:512],
                "monitor_model": monitor_models[0] if monitor_models else "",
                "monitor_endpoint": str(getattr(primary_monitor, "endpoint", "") or ""),
                "monitor_mode": monitor_mode,
                "last_checked_at": _as_iso(getattr(primary_monitor, "last_checked_at", None)),
                "requests_5m": traffic["requests"],
                "failed_requests_5m": traffic["failed_requests"],
                "fallback_requests_5m": traffic["fallback_requests"],
                "fallback_rate_5m": traffic["fallback_rate"],
                "avg_latency_ms_5m": traffic["avg_latency_ms"],
                "max_latency_ms_5m": traffic["max_latency_ms"],
                "cooldown_until": _as_iso(getattr(runtime_state, "cooldown_until", None)),
                "last_error_code": str(getattr(runtime_state, "last_error_code", "") or ""),
            }
        )

    model_payloads: list[dict[str, Any]] = []
    for public_model_id in sorted(routes_by_model):
        model_routes = routes_by_model[public_model_id]
        active_routes = [
            route for route in model_routes
            if str(getattr(route, "status", "") or "").lower() == "active"
            and str(getattr(channel_by_id.get(str(route.channel_id)), "status", "") or "").lower() == "active"
        ]
        traffic = traffic_by_model.get(public_model_id, _empty_traffic())
        route_payloads: list[dict[str, Any]] = []
        route_statuses: list[str] = []
        for route in sorted(
            active_routes,
            key=lambda row: (
                int(row.priority_override if row.priority_override is not None else getattr(channel_by_id.get(str(row.channel_id)), "priority", 0) or 0),
                str(row.id),
            ),
        ):
            channel = channel_by_id.get(str(route.channel_id))
            route_traffic = traffic_by_model_channel.get(
                (public_model_id, str(route.channel_id)),
                _empty_traffic(),
            )
            if _runtime_cooling(runtime_by_channel.get(str(route.channel_id)), now):
                route_status = "cooling"
            elif route_traffic["failed_requests"] > 0 or route_traffic["fallback_rate"] >= 0.05:
                route_status = "degraded"
            else:
                route_status = "operational"
            route_statuses.append(route_status)
            route_payloads.append(
                {
                    "id": str(route.id),
                    "channel_id": str(route.channel_id),
                    "channel_name": str(getattr(channel, "name", "") or route.channel_id),
                    "endpoint": str(getattr(route, "endpoint", "") or ""),
                    "upstream_model": str(getattr(route, "upstream_model", "") or public_model_id),
                    "priority": int(route.priority_override if route.priority_override is not None else getattr(channel, "priority", 0) or 0),
                    "weight": max(1, int(route.weight_override if route.weight_override is not None else getattr(channel, "weight", 1) or 1)),
                    "health_status": route_status,
                    "requests_5m": route_traffic["requests"],
                    "failed_requests_5m": route_traffic["failed_requests"],
                    "fallback_rate_5m": route_traffic["fallback_rate"],
                    "avg_latency_ms_5m": route_traffic["avg_latency_ms"],
                }
            )

        if not active_routes:
            health_status = "failed"
        elif all(status == "cooling" for status in route_statuses):
            health_status = "cooling"
        elif any(status in {"cooling", "degraded"} for status in route_statuses):
            health_status = "degraded"
        elif traffic["failed_requests"] > 0 or traffic["fallback_rate"] >= 0.05:
            health_status = "degraded"
        else:
            health_status = "operational"

        model_payloads.append(
            {
                "public_model_id": public_model_id,
                "health_status": health_status,
                "route_count": len(model_routes),
                "active_route_count": len(active_routes),
                "requests_5m": traffic["requests"],
                "success_requests_5m": traffic["success_requests"],
                "failed_requests_5m": traffic["failed_requests"],
                "fallback_requests_5m": traffic["fallback_requests"],
                "fallback_rate_5m": traffic["fallback_rate"],
                "avg_latency_ms_5m": traffic["avg_latency_ms"],
                "max_latency_ms_5m": traffic["max_latency_ms"],
                "last_seen_at": traffic["last_seen_at"],
                "routes": route_payloads,
            }
        )

    incidents: list[dict[str, Any]] = []
    for channel in channel_payloads:
        if channel["health_status"] not in {"failed", "cooling", "degraded"}:
            continue
        incidents.append(
            {
                "id": f"channel:{channel['id']}",
                "severity": "critical" if channel["health_status"] == "failed" else "warning",
                "scope": "channel",
                "channel_id": channel["id"],
                "channel_name": channel["name"],
                "status": channel["health_status"],
                "message": channel["monitor_message"] or channel["last_error_code"] or "服务通道状态异常",
                "started_at": channel["last_checked_at"] or channel["cooldown_until"],
            }
        )

    recent_failure_payloads = [
        {
            "id": str(getattr(row, "id", "") or ""),
            "created_at": _as_iso(getattr(row, "created_at", None)),
            "endpoint": str(getattr(row, "endpoint", "") or ""),
            "model": str(getattr(row, "resolved_public_model", "") or getattr(row, "model", "") or ""),
            "status_code": int(getattr(row, "status_code", 0) or 0),
            "duration_ms": int(getattr(row, "duration_ms", 0) or 0),
            "channel_id": str(getattr(row, "channel_id", "") or ""),
            "route_reason": str(getattr(row, "route_reason", "") or "")[:128],
            "route_attempt": int(getattr(row, "route_attempt", 0) or 0),
        }
        for row in recent_failures
    ]

    relevant_channels = [item for item in channel_payloads if item["configured_status"] == "active"]
    overall_status = _worst_status(
        (item["health_status"] for item in relevant_channels),
        default="disabled" if channel_payloads else "pending",
    )
    total_requests = sum(item["requests_5m"] for item in model_payloads)
    total_fallbacks = sum(item["fallback_requests_5m"] for item in model_payloads)
    total_failures = sum(item["failed_requests_5m"] for item in model_payloads)
    return {
        "generated_at": now.isoformat(),
        "cache_ttl_seconds": RELIABILITY_CACHE_TTL_SECONDS,
        "overall": {
            "health_status": overall_status,
            "channels_total": len(channel_payloads),
            "channels_operational": sum(1 for item in channel_payloads if item["health_status"] == "operational"),
            "channels_affected": sum(1 for item in channel_payloads if item["health_status"] in {"degraded", "cooling", "failed"}),
            "models_total": len(model_payloads),
            "models_operational": sum(1 for item in model_payloads if item["health_status"] == "operational"),
            "models_affected": sum(1 for item in model_payloads if item["health_status"] in {"degraded", "cooling", "failed"}),
            "requests_5m": total_requests,
            "failed_requests_5m": total_failures,
            "fallback_requests_5m": total_fallbacks,
            "fallback_rate_5m": round(total_fallbacks / total_requests, 4) if total_requests else 0.0,
            "active_incidents": len(incidents),
        },
        "models": model_payloads,
        "channels": channel_payloads,
        "incidents": incidents,
        "recent_failures": recent_failure_payloads,
    }


class SimpleTrafficRow:
    requests = 0
    success_requests = 0
    failed_requests = 0
    fallback_requests = 0
    avg_latency_ms = 0
    max_latency_ms = 0
    last_seen_at = None


async def build_reliability_overview(db: AsyncSession) -> dict[str, Any]:
    now = _utcnow()
    since = now - timedelta(minutes=5)
    public_model_expr = func.coalesce(
        func.nullif(RequestLog.resolved_public_model, ""),
        func.nullif(RequestLog.model, ""),
        "",
    )

    channels = (
        await db.execute(select(ProviderChannel).order_by(ProviderChannel.priority.asc(), ProviderChannel.name.asc()))
    ).scalars().all()
    routes = (
        await db.execute(select(ModelChannelRoute).order_by(ModelChannelRoute.public_model_id.asc(), ModelChannelRoute.created_at.asc()))
    ).scalars().all()
    runtime_states = (await db.execute(select(ProviderChannelRuntimeState))).scalars().all()
    monitors = (
        await db.execute(select(ProviderChannelMonitor).order_by(ProviderChannelMonitor.created_at.asc()))
    ).scalars().all()
    traffic_rows = (
        await db.execute(
            select(
                public_model_expr.label("public_model_id"),
                RequestLog.channel_id.label("channel_id"),
                func.count(RequestLog.id).label("requests"),
                func.coalesce(func.sum(case((RequestLog.status_code < 400, 1), else_=0)), 0).label("success_requests"),
                func.coalesce(func.sum(case((RequestLog.status_code >= 400, 1), else_=0)), 0).label("failed_requests"),
                func.coalesce(func.sum(case((RequestLog.route_attempt > 0, 1), else_=0)), 0).label("fallback_requests"),
                func.coalesce(func.avg(RequestLog.duration_ms), 0).label("avg_latency_ms"),
                func.coalesce(func.max(RequestLog.duration_ms), 0).label("max_latency_ms"),
                func.max(RequestLog.created_at).label("last_seen_at"),
            )
            .where(RequestLog.created_at >= since, RequestLog.channel_id != "")
            .group_by(public_model_expr, RequestLog.channel_id)
        )
    ).all()
    recent_failures = (
        await db.execute(
            select(RequestLog)
            .where(RequestLog.created_at >= since, RequestLog.status_code >= 400)
            .order_by(RequestLog.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    return assemble_reliability_overview(
        channels=channels,
        routes=routes,
        runtime_states=runtime_states,
        monitors=monitors,
        traffic_rows=traffic_rows,
        recent_failures=recent_failures,
        now=now,
    )


def invalidate_reliability_cache() -> None:
    global _CACHE_VALUE, _CACHE_EXPIRES_AT
    _CACHE_VALUE = None
    _CACHE_EXPIRES_AT = 0.0


async def get_cached_reliability_overview(db: AsyncSession) -> dict[str, Any]:
    global _CACHE_VALUE, _CACHE_EXPIRES_AT
    now = time.monotonic()
    if _CACHE_VALUE is not None and now < _CACHE_EXPIRES_AT:
        return _CACHE_VALUE

    async with _CACHE_LOCK:
        now = time.monotonic()
        if _CACHE_VALUE is not None and now < _CACHE_EXPIRES_AT:
            return _CACHE_VALUE
        payload = await build_reliability_overview(db)
        _CACHE_VALUE = payload
        _CACHE_EXPIRES_AT = now + RELIABILITY_CACHE_TTL_SECONDS
        return payload


def reliability_guard(request: Request) -> None:
    require_admin(request)


@router.get("/overview", dependencies=[Depends(reliability_guard)])
async def reliability_overview(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await get_cached_reliability_overview(db)
