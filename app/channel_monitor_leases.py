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
    claimed_until = getattr(monitor, "claimed_until", None)
    if claimed_until is not None and claimed_until > now:
        raise ProviderChannelMonitorClaimedError(f"channel monitor {monitor_id} is already claimed")
    monitor.claimed_until = now + timedelta(seconds=monitor_claim_lease_seconds(monitor))
    await db.commit()
    return monitor
