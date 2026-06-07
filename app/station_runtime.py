from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Station, StationAlias, StationCustomerLink, StationPricebookEntry
from .router import ResolvedModel, registry as model_registry
from .usage_buffer import calculate_cost_cents, calculate_image_cost_cents


@dataclass(frozen=True)
class StationResolvedModel:
    resolved_model: ResolvedModel
    display_model: str
    station_id: str
    station_alias: str
    resolved_public_model: str
    retail_input_per_million: int
    retail_output_per_million: int
    retail_price_per_image_cents: float
    wholesale_input_per_million: int
    wholesale_output_per_million: int
    wholesale_price_per_image_cents: float
    price_version: int


def _station_context(user: Any) -> dict:
    context = getattr(user, "_station_context", None)
    return context if isinstance(context, dict) else {}


def station_context_from_link(station: Any, link: Any | None = None) -> dict:
    return {
        "station_id": getattr(station, "id", "") or "",
        "slug": getattr(station, "slug", "") or "",
        "display_name": getattr(station, "display_name", "") or "",
        "status": getattr(station, "status", "active") or "active",
        "mode": getattr(station, "mode", "commission_station") or "commission_station",
        "default_text_alias": getattr(station, "default_text_alias", "") or "",
        "default_image_alias": getattr(station, "default_image_alias", "") or "",
        "link_id": getattr(link, "id", "") if link is not None else "",
        "link_status": getattr(link, "status", "") if link is not None else "",
    }


async def station_context_for_user(db: AsyncSession, user_id: str) -> dict:
    user_id = (user_id or "").strip()
    if not user_id:
        return {}
    station_row = (
        await db.execute(
            select(StationCustomerLink, Station)
            .join(Station, StationCustomerLink.station_id == Station.id)
            .where(StationCustomerLink.user_id == user_id)
            .limit(1)
        )
    ).first()
    if not station_row:
        return {}
    link, station = station_row
    return station_context_from_link(station, link)


def user_station_context(user: Any) -> dict:
    return _station_context(user)


def _default_alias_for_endpoint(context: dict, endpoint: str) -> str:
    if endpoint.startswith("images/"):
        return str(context.get("default_image_alias") or "").strip()
    return str(context.get("default_text_alias") or "").strip()


def _validate_station_active(context: dict) -> None:
    if not context:
        return
    station_status = str(context.get("status") or "active").strip().lower()
    link_status = str(context.get("link_status") or "active").strip().lower()
    if station_status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="station suspended")
    if link_status and link_status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="station customer disabled")


async def resolve_station_model_for_user(
    db: AsyncSession,
    user: Any,
    requested_model: str,
    endpoint: str,
    messages: list | None = None,
    tools: list | None = None,
) -> StationResolvedModel | None:
    context = _station_context(user)
    if not context:
        return None
    _validate_station_active(context)

    station_id = str(context.get("station_id") or "").strip()
    station_alias = (requested_model or "").strip() or _default_alias_for_endpoint(context, endpoint)
    if not station_id or not station_alias:
        return None

    alias = (
        await db.execute(
            select(StationAlias)
            .where(
                StationAlias.station_id == station_id,
                StationAlias.alias == station_alias,
                StationAlias.status == "active",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not alias:
        return None

    resolved_model = model_registry.resolve_public_model(
        alias.target_public_model_id,
        endpoint,
        messages,
        tools,
    )
    public_model = resolved_model.public_model

    price = (
        await db.execute(
            select(StationPricebookEntry)
            .where(
                StationPricebookEntry.station_id == station_id,
                StationPricebookEntry.station_alias_id == alias.id,
                StationPricebookEntry.status == "active",
            )
            .order_by(StationPricebookEntry.price_version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    retail_input = int(
        getattr(price, "retail_input_per_million_cents", 0) or public_model.price_input_per_million or 0
    )
    retail_output = int(
        getattr(price, "retail_output_per_million_cents", 0) or public_model.price_output_per_million or 0
    )
    retail_image = float(
        getattr(price, "retail_price_per_image_cents", 0.0) or public_model.price_per_image_cents or 0.0
    )
    price_version = int(getattr(price, "price_version", 0) or 0)

    return StationResolvedModel(
        resolved_model=resolved_model,
        display_model=station_alias,
        station_id=station_id,
        station_alias=station_alias,
        resolved_public_model=public_model.public_id,
        retail_input_per_million=retail_input,
        retail_output_per_million=retail_output,
        retail_price_per_image_cents=retail_image,
        wholesale_input_per_million=int(public_model.price_input_per_million or 0),
        wholesale_output_per_million=int(public_model.price_output_per_million or 0),
        wholesale_price_per_image_cents=float(public_model.price_per_image_cents or 0.0),
        price_version=price_version,
    )


def station_usage_kwargs(station_model: StationResolvedModel | None) -> dict:
    if station_model is None:
        return {}
    return {
        "station_id": station_model.station_id,
        "station_alias": station_model.station_alias,
        "resolved_public_model": station_model.resolved_public_model,
        "wholesale_price_input_per_million": station_model.wholesale_input_per_million,
        "wholesale_price_output_per_million": station_model.wholesale_output_per_million,
        "wholesale_price_per_image_cents": station_model.wholesale_price_per_image_cents,
        "price_version": station_model.price_version,
    }


def public_model_pricing_kwargs(public_model: Any) -> dict:
    if public_model is None:
        return {}
    return {
        "pricing_mode": getattr(public_model, "pricing_mode", "") or "",
        "model_multiplier": getattr(public_model, "model_multiplier", 1.0) or 1.0,
        "output_multiplier": getattr(public_model, "output_multiplier", 1.0) or 1.0,
        "cache_read_multiplier": getattr(public_model, "cache_read_multiplier", 0.0) or 0.0,
        "image_multiplier": getattr(public_model, "image_multiplier", 1.0) or 1.0,
        "video_multiplier": getattr(public_model, "video_multiplier", 1.0) or 1.0,
        "base_price_input_per_million": getattr(public_model, "base_price_input_per_million", 0) or 0,
        "base_price_output_per_million": getattr(public_model, "base_price_output_per_million", 0) or 0,
        "base_price_per_image_cents": getattr(public_model, "base_price_per_image_cents", 0.0) or 0.0,
        "base_price_per_video_cents": getattr(public_model, "base_price_per_video_cents", 0.0) or 0.0,
        "effective_cached_input_per_million": getattr(public_model, "effective_cached_input_per_million", 0.0) or 0.0,
        "price_version": getattr(public_model, "price_version", 0) or 0,
    }


def usage_pricing_kwargs(
    public_model: Any,
    station_model: StationResolvedModel | None = None,
    user_cache_read_multiplier_override: float | None = None,
) -> dict:
    """Merge pricing audit fields for request logging.

    Station pricebook versions keep their existing RequestLog.price_version
    meaning, so station metadata intentionally wins over the public model
    pricing version when both are present.
    """
    payload = public_model_pricing_kwargs(public_model)
    if user_cache_read_multiplier_override is not None:
        payload["cache_read_multiplier"] = float(user_cache_read_multiplier_override)
        if station_model is not None:
            input_price = float(station_model.retail_input_per_million or 0)
        else:
            input_price = float(getattr(public_model, "price_input_per_million", 0) or 0)
        payload["effective_cached_input_per_million"] = round(
            input_price * float(user_cache_read_multiplier_override),
            4,
        )
    payload.update(station_usage_kwargs(station_model))
    return payload


def calculate_station_wholesale_cost(
    *,
    station_model: StationResolvedModel | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    usage_unit_type: str = "tokens",
    usage_unit_count: int = 0,
    image_count: int = 0,
) -> float:
    if station_model is None:
        return 0.0
    if (usage_unit_type or "tokens") == "images":
        return calculate_image_cost_cents(
            image_count=image_count or usage_unit_count,
            price_per_image_cents=station_model.wholesale_price_per_image_cents,
        )
    return calculate_cost_cents(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        price_input_per_million=station_model.wholesale_input_per_million,
        price_output_per_million=station_model.wholesale_output_per_million,
    )
