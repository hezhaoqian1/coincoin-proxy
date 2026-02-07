import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, Tuple

from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from .db import SessionLocal
from .models import UsageDaily, User


logger = logging.getLogger("coincoin.usage")


class UsageBuffer:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._daily: Dict[Tuple[str, date], Dict[str, int]] = defaultdict(lambda: {"tokens": 0, "requests": 0})
        self._tokens_by_user: Dict[str, int] = defaultdict(int)

    async def add(self, user_id: str, tokens: int, requests: int) -> None:
        if tokens == 0 and requests == 0:
            return
        day = date.today()
        async with self._lock:
            bucket = self._daily[(user_id, day)]
            bucket["tokens"] += int(tokens)
            bucket["requests"] += int(requests)
            self._tokens_by_user[user_id] += int(tokens)

    async def get_pending_tokens(self, user_id: str) -> int:
        async with self._lock:
            return int(self._tokens_by_user.get(user_id, 0))

    async def get_pending_requests_today(self, user_id: str) -> int:
        today = date.today()
        async with self._lock:
            bucket = self._daily.get((user_id, today))
            if not bucket:
                return 0
            return int(bucket.get("requests", 0))

    async def snapshot_and_reset(self):
        async with self._lock:
            daily = dict(self._daily)
            tokens_by_user = dict(self._tokens_by_user)
            self._daily.clear()
            self._tokens_by_user.clear()
        return daily, tokens_by_user

    async def requeue(self, daily, tokens_by_user) -> None:
        async with self._lock:
            for key, stats in daily.items():
                bucket = self._daily[key]
                bucket["tokens"] += int(stats.get("tokens", 0))
                bucket["requests"] += int(stats.get("requests", 0))
            for user_id, tokens in tokens_by_user.items():
                self._tokens_by_user[user_id] += int(tokens)


usage_buffer = UsageBuffer()


async def flush_once() -> None:
    daily, tokens_by_user = await usage_buffer.snapshot_and_reset()
    if not daily and not tokens_by_user:
        return

    try:
        async with SessionLocal() as session:
            for user_id, tokens in tokens_by_user.items():
                if tokens:
                    await session.execute(
                        update(User)
                        .where(User.id == user_id)
                        .values(token_used=User.token_used + tokens)
                    )

            for (user_id, day), stats in daily.items():
                tokens = int(stats.get("tokens", 0))
                requests = int(stats.get("requests", 0))
                if tokens == 0 and requests == 0:
                    continue
                stmt = mysql_insert(UsageDaily).values(
                    user_id=user_id,
                    day=day,
                    tokens_total=tokens,
                    requests_total=requests,
                    updated_at=datetime.utcnow(),
                )
                stmt = stmt.on_duplicate_key_update(
                    tokens_total=UsageDaily.tokens_total + tokens,
                    requests_total=UsageDaily.requests_total + requests,
                    updated_at=datetime.utcnow(),
                )
                await session.execute(stmt)

            await session.commit()
    except Exception:
        logger.exception("usage flush failed; re-queueing")
        await usage_buffer.requeue(daily, tokens_by_user)


async def flush_loop(interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await flush_once()
