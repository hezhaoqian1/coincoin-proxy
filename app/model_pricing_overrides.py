from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ModelPricingOverride
from .router import registry


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def override_rows_to_snapshot(rows: Iterable[ModelPricingOverride]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    overrides: Dict[str, Dict[str, Any]] = {}
    version = 0
    for row in rows:
        model_id = str(getattr(row, "model_id", "") or "").strip()
        if not model_id:
            continue
        updated_at = getattr(row, "updated_at", None)
        if isinstance(updated_at, datetime):
            version = max(version, int(updated_at.timestamp() * 1000))
        row_version = int(getattr(row, "price_version", 0) or 0)
        version = max(version, row_version)
        overrides[model_id] = {
            "pricing_mode": str(getattr(row, "pricing_mode", "") or "multiplier").strip() or "multiplier",
            "model_multiplier": _as_float(getattr(row, "model_multiplier", 1.0), 1.0),
            "output_multiplier": _as_float(getattr(row, "output_multiplier", 1.0), 1.0),
            "cache_read_multiplier": _as_float(getattr(row, "cache_read_multiplier", 0.0), 0.0),
            "image_multiplier": _as_float(getattr(row, "image_multiplier", 1.0), 1.0),
            "video_multiplier": _as_float(getattr(row, "video_multiplier", 1.0), 1.0),
            "price_version": row_version,
        }
    return overrides, version


async def get_model_pricing_override_db_state(db: AsyncSession) -> Tuple[int, str]:
    row = (
        await db.execute(
            select(
                func.count(ModelPricingOverride.model_id),
                func.coalesce(func.max(ModelPricingOverride.updated_at), datetime(1970, 1, 1)),
            )
        )
    ).one()
    return int(row[0] or 0), str(row[1] or "")


async def refresh_model_pricing_registry_from_db(db: AsyncSession) -> None:
    rows = (await db.execute(select(ModelPricingOverride))).scalars().all()
    overrides, version = override_rows_to_snapshot(rows)
    registry.set_runtime_pricing_overrides(overrides, version=version)
