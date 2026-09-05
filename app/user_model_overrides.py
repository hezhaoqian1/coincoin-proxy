from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserModelPricingOverride, UserModelRoutingOverride
from .router import ModelConfig, ResolvedModel

USER_MODEL_ROUTING_ATTR = "_model_routing_overrides"
USER_MODEL_PRICING_ATTR = "_model_pricing_overrides"


def _timestamp_us(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1_000_000)
    return 0


def routing_override_rows_to_snapshot(
    rows: Iterable[UserModelRoutingOverride],
) -> Tuple[Dict[str, Dict[str, Any]], int]:
    overrides: Dict[str, Dict[str, Any]] = {}
    version = 0
    for row in rows:
        public_model_id = str(getattr(row, "public_model_id", "") or "").strip()
        if not public_model_id:
            continue
        version = max(version, _timestamp_us(getattr(row, "updated_at", None)))
        provider_model = str(getattr(row, "provider_model", "") or "").strip()
        upstream_model = str(getattr(row, "upstream_model", "") or "").strip()
        overrides[public_model_id] = {
            "public_model_id": public_model_id,
            "provider_model": provider_model,
            "upstream_model": upstream_model or provider_model,
            "enabled": bool(getattr(row, "enabled", 1)),
            "updated_by": str(getattr(row, "updated_by", "") or "").strip(),
            "updated_at": getattr(row, "updated_at", None),
        }
    return overrides, version


def pricing_override_rows_to_snapshot(
    rows: Iterable[UserModelPricingOverride],
) -> Tuple[Dict[str, Dict[str, Any]], int]:
    overrides: Dict[str, Dict[str, Any]] = {}
    version = 0
    for row in rows:
        public_model_id = str(getattr(row, "public_model_id", "") or "").strip()
        if not public_model_id:
            continue
        version = max(version, _timestamp_us(getattr(row, "updated_at", None)))
        multiplier = getattr(row, "cache_read_multiplier_override", None)
        overrides[public_model_id] = {
            "public_model_id": public_model_id,
            "cache_read_multiplier_override": None if multiplier is None else float(multiplier),
            "updated_by": str(getattr(row, "updated_by", "") or "").strip(),
            "updated_at": getattr(row, "updated_at", None),
        }
    return overrides, version


async def load_user_model_overrides_from_db(
    db: AsyncSession,
    user_id: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], int]:
    routing_rows = (
        await db.execute(
            select(UserModelRoutingOverride).where(UserModelRoutingOverride.user_id == user_id)
        )
    ).scalars().all()
    pricing_rows = (
        await db.execute(
            select(UserModelPricingOverride).where(UserModelPricingOverride.user_id == user_id)
        )
    ).scalars().all()
    routing_overrides, routing_version = routing_override_rows_to_snapshot(routing_rows)
    pricing_overrides, pricing_version = pricing_override_rows_to_snapshot(pricing_rows)
    return routing_overrides, pricing_overrides, max(routing_version, pricing_version)


def user_routing_overrides(user: Any) -> Dict[str, Dict[str, Any]]:
    raw = getattr(user, USER_MODEL_ROUTING_ATTR, None)
    return raw if isinstance(raw, dict) else {}


def user_pricing_overrides(user: Any) -> Dict[str, Dict[str, Any]]:
    raw = getattr(user, USER_MODEL_PRICING_ATTR, None)
    return raw if isinstance(raw, dict) else {}


def current_user_routing_override(user: Any, public_model_id: str) -> Optional[Dict[str, Any]]:
    override = user_routing_overrides(user).get(str(public_model_id or "").strip())
    if not isinstance(override, dict):
        return None
    if not override.get("enabled", True):
        return None
    target_model = str(override.get("upstream_model") or override.get("provider_model") or "").strip()
    if not target_model:
        return None
    return override


def current_user_cache_read_multiplier_override(user: Any, public_model_id: str) -> Optional[float]:
    override = user_pricing_overrides(user).get(str(public_model_id or "").strip())
    if not isinstance(override, dict):
        return None
    multiplier = override.get("cache_read_multiplier_override", None)
    if multiplier is None:
        return None
    try:
        parsed = float(multiplier)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def apply_user_routing_override_to_backend(
    public_model: Any,
    backend: ModelConfig,
    user: Any,
    *,
    route_reason: str,
) -> Tuple[ModelConfig, str, Optional[Dict[str, Any]]]:
    override = current_user_routing_override(user, getattr(public_model, "public_id", ""))
    if override is None:
        return backend, route_reason, None
    target_model = str(override.get("upstream_model") or override.get("provider_model") or "").strip()
    if not target_model:
        return backend, route_reason, None
    updated_backend = replace(backend, model_id=target_model)
    updated_route_reason = route_reason if ":user_override" in route_reason else f"{route_reason}:user_override"
    return updated_backend, updated_route_reason, override


def apply_user_routing_override_to_resolved_model(
    resolved_model: ResolvedModel,
    user: Any,
) -> Tuple[ResolvedModel, Optional[Dict[str, Any]]]:
    backend, route_reason, override = apply_user_routing_override_to_backend(
        resolved_model.public_model,
        resolved_model.backend,
        user,
        route_reason=resolved_model.route_reason,
    )
    if override is None:
        return resolved_model, None
    public_model = replace(
        resolved_model.public_model,
        provider_model=str(override.get("provider_model") or backend.model_id or "").strip(),
        upstream_model=str(override.get("upstream_model") or override.get("provider_model") or backend.model_id or "").strip(),
    )
    return (
        replace(
            resolved_model,
            public_model=public_model,
            backend=backend,
            route_reason=route_reason,
            lock_model_selection=True,
        ),
        override,
    )


def effective_provider_model_name(
    public_model: Any,
    backend: ModelConfig,
    override: Optional[Dict[str, Any]] = None,
) -> str:
    if isinstance(override, dict):
        provider_model = str(override.get("provider_model") or "").strip()
        if provider_model:
            return provider_model
    backend_model = str(getattr(backend, "model_id", "") or "").strip()
    if backend_model:
        return backend_model
    provider_model = str(getattr(public_model, "provider_model", "") or "").strip()
    return provider_model or backend_model


def apply_user_overrides_to_resolution(
    user: Any,
    resolved_model: ResolvedModel,
    station_model: Any = None,
) -> Tuple[ResolvedModel, Any, Optional[Dict[str, Any]], Optional[float], str]:
    updated_resolved_model, routing_override = apply_user_routing_override_to_resolved_model(
        resolved_model,
        user,
    )
    updated_station_model = station_model
    if station_model is not None and routing_override is not None:
        updated_station_model = replace(station_model, resolved_model=updated_resolved_model)
    cache_multiplier_override = current_user_cache_read_multiplier_override(
        user,
        updated_resolved_model.public_model.public_id,
    )
    provider_model = effective_provider_model_name(
        updated_resolved_model.public_model,
        updated_resolved_model.backend,
        routing_override,
    )
    return (
        updated_resolved_model,
        updated_station_model,
        routing_override,
        cache_multiplier_override,
        provider_model,
    )
