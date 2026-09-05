from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import ProviderChannelMonitor


class ProviderChannelMonitorClaimedError(RuntimeError):
    pass


def monitor_claim_lease_seconds(monitor: ProviderChannelMonitor) -> int:
    timeout = max(5, int(monitor.timeout_seconds or settings.provider_channel_monitor_default_timeout))
    return max(60, timeout + 30)


def monitor_has_active_claim(
    monitor: ProviderChannelMonitor,
    *,
    now: datetime | None = None,
) -> bool:
    claimed_until = getattr(monitor, "claimed_until", None)
    if claimed_until is None:
        return False
    now = now or datetime.now(UTC).replace(tzinfo=None)
    if claimed_until.tzinfo is not None:
        claimed_until = claimed_until.astimezone(UTC).replace(tzinfo=None)
    return claimed_until > now


async def claim_provider_channel_monitor_for_run(
    db: AsyncSession,
    monitor_id: str,
) -> ProviderChannelMonitor | None:
    monitor = await db.scalar(
        select(ProviderChannelMonitor)
        .where(ProviderChannelMonitor.id == monitor_id)
        .with_for_update()
    )
    if monitor is None:
        return None
    now = datetime.now(UTC).replace(tzinfo=None)
    if monitor_has_active_claim(monitor, now=now):
        raise ProviderChannelMonitorClaimedError(f"channel monitor {monitor_id} is already claimed")
    monitor.claimed_until = now + timedelta(seconds=monitor_claim_lease_seconds(monitor))
    await db.commit()
    return monitor
