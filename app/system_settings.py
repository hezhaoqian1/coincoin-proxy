from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SystemSetting
from .router import registry as model_registry


CLAUDE_COMPAT_PROVIDER_KEY = "claude_compat_provider"
ALERT_RUNTIME_SETTING_KEYS = frozenset(
    {
        "fallback_alert_webhook_url",
        "fallback_alert_enabled",
        "upstream_failure_alert_threshold",
        "upstream_auth_alert_threshold",
        "upstream_failure_alert_window_seconds",
        "upstream_failure_alert_dedup_seconds",
        "fallback_alert_max_pending_tasks",
    }
)
SUPPORTED_RUNTIME_SETTING_KEYS = frozenset({CLAUDE_COMPAT_PROVIDER_KEY, *ALERT_RUNTIME_SETTING_KEYS})


def _apply_alert_runtime_settings(runtime_settings: Dict[str, str]) -> None:
    from .fallback_alerts import set_runtime_alert_settings

    set_runtime_alert_settings(runtime_settings)


def _timestamp_us(value: Any) -> int:
    if value is not None and hasattr(value, "timestamp"):
        return int(value.timestamp() * 1_000_000)
    return 0


def system_setting_rows_to_snapshot(rows: Iterable[SystemSetting]) -> Tuple[Dict[str, str], int]:
    snapshot: Dict[str, str] = {}
    version = 0
    for row in rows:
        key = str(getattr(row, "setting_key", "") or "").strip()
        if key not in SUPPORTED_RUNTIME_SETTING_KEYS:
            continue
        snapshot[key] = str(getattr(row, "setting_value", "") or "").strip()
        version = max(version, _timestamp_us(getattr(row, "updated_at", None)))
    return snapshot, version


async def load_runtime_system_settings_from_db(db: AsyncSession) -> Tuple[Dict[str, str], int]:
    result = await db.execute(select(SystemSetting))
    return system_setting_rows_to_snapshot(result.scalars().all())


async def get_runtime_system_settings_db_state(db: AsyncSession) -> Tuple[Tuple[str, str], ...]:
    result = await db.execute(select(SystemSetting))
    snapshot, _ = system_setting_rows_to_snapshot(result.scalars().all())
    return tuple(sorted(snapshot.items()))


async def refresh_runtime_system_settings_from_db(db: AsyncSession) -> None:
    runtime_settings, version = await load_runtime_system_settings_from_db(db)
    apply_runtime_system_settings(
        runtime_settings,
        replace=True,
        version=version,
    )


def apply_runtime_system_settings(
    runtime_settings: Dict[str, Any],
    *,
    replace: bool = False,
    version: int | None = None,
) -> bool:
    current_version = int(
        getattr(model_registry, "_runtime_system_settings_version", 0) or 0
    )
    if version is not None and int(version) < current_version:
        return False

    merged_settings = {} if replace else model_registry.current_system_settings()
    merged_settings.update(
        {
            key: str(value or "").strip()
            for key, value in (runtime_settings or {}).items()
            if key in SUPPORTED_RUNTIME_SETTING_KEYS
        }
    )
    next_version = (
        int(version)
        if version is not None
        else max(int(time.time() * 1_000_000), current_version + 1)
    )
    _apply_alert_runtime_settings(merged_settings)
    model_registry.set_runtime_system_settings(merged_settings, version=next_version)
    model_registry.init_from_settings()
    return True


def apply_runtime_system_setting(setting_key: str, setting_value: str) -> None:
    if setting_key not in SUPPORTED_RUNTIME_SETTING_KEYS:
        return
    apply_runtime_system_settings({setting_key: setting_value})
