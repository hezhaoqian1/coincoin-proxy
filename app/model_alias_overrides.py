from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ModelAliasOverride
from .router import registry as model_registry


def _timestamp_us(value: Any) -> int:
    if value is not None and hasattr(value, "timestamp"):
        return int(value.timestamp() * 1_000_000)
    return 0


def override_rows_to_snapshot(rows: Iterable[ModelAliasOverride]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    overrides: Dict[str, Dict[str, Any]] = {}
    version = 0
    for row in rows:
        alias_id = str(getattr(row, "alias_id", "") or "").strip()
        if not alias_id:
            continue
        version = max(version, _timestamp_us(getattr(row, "updated_at", None)))
        item: Dict[str, Any] = {"enabled": bool(getattr(row, "enabled", 1))}
        provider_model = str(getattr(row, "provider_model", "") or "").strip()
        upstream_model = str(getattr(row, "upstream_model", "") or "").strip()
        if provider_model:
            item["provider_model"] = provider_model
        if upstream_model:
            item["upstream_model"] = upstream_model
        overrides[alias_id] = item
    return overrides, version


async def load_model_alias_overrides_from_db(db: AsyncSession) -> Tuple[Dict[str, Dict[str, Any]], int]:
    result = await db.execute(select(ModelAliasOverride))
    return override_rows_to_snapshot(result.scalars().all())


async def get_model_alias_override_db_state(db: AsyncSession) -> Tuple[int, int]:
    result = await db.execute(select(func.count(ModelAliasOverride.alias_id), func.max(ModelAliasOverride.updated_at)))
    row = result.first()
    if not row:
        return (0, 0)
    count, updated_at = row
    return (int(count or 0), _timestamp_us(updated_at))


async def refresh_model_alias_registry_from_db(db: AsyncSession) -> None:
    overrides, version = await load_model_alias_overrides_from_db(db)
    model_registry.set_runtime_alias_overrides(overrides, version=version)
    model_registry.init_from_settings()


def apply_runtime_alias_override(alias_id: str, override: Dict[str, Any]) -> None:
    overrides = dict(model_registry.alias_overrides)
    overrides[alias_id] = {**(overrides.get(alias_id) or {}), **override}
    version = max(int(time.time() * 1_000_000), getattr(model_registry, "_runtime_alias_override_version", 0) + 1)
    model_registry.set_runtime_alias_overrides(overrides, version=version)
    model_registry.init_from_settings()


def clear_runtime_alias_override(alias_id: str) -> None:
    overrides = dict(model_registry.alias_overrides)
    overrides.pop(alias_id, None)
    version = max(int(time.time() * 1_000_000), getattr(model_registry, "_runtime_alias_override_version", 0) + 1)
    model_registry.set_runtime_alias_overrides(overrides, version=version)
    model_registry.init_from_settings()
