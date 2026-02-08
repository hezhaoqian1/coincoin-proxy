import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, Tuple

from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from .config import settings
from .db import SessionLocal
from .models import UsageDaily, User


logger = logging.getLogger("coincoin.usage")


def calculate_cost_cents(input_tokens: int, output_tokens: int) -> float:
    """计算消费金额（单位：分，保留小数精度）
    
    价格配置单位是 分/百万tokens
    计算公式: (tokens / 1_000_000) * price_per_million
    
    注：返回浮点数，保留精度以避免小请求被舍入到 0
    最终在 flush 时累积后再取整
    """
    input_cost = (input_tokens * settings.price_input_per_million) / 1_000_000
    output_cost = (output_tokens * settings.price_output_per_million) / 1_000_000
    return input_cost + output_cost


class UsageBuffer:
    """高性能使用量缓冲区
    
    设计原则：
    1. 写入路径（add）极快：只做内存操作，不阻塞请求
    2. 读取路径（get_pending_*）尽量快：简单字典查询
    3. 批量刷盘：后台任务定期将累计数据写入 DB
    
    性能特点：
    - add() 耗时 < 1ms（锁内操作 < 100μs）
    - 使用分片锁减少竞争（按 user_id hash 分配到不同锁）
    """
    
    # 分片数量，减少锁竞争
    SHARD_COUNT = 16
    
    def __init__(self) -> None:
        # 分片锁：减少高并发时的锁竞争
        self._locks = [asyncio.Lock() for _ in range(self.SHARD_COUNT)]
        # 每日统计: (user_id, date) -> {input_tokens, output_tokens, requests, cost_cents_f}
        # cost_cents_f 使用 float 保留精度
        self._daily: Dict[Tuple[str, date], Dict[str, float]] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0, "requests": 0, "cost_cents_f": 0.0}
        )
        # 用户累计: user_id -> {input_tokens, output_tokens, cost_cents_f}
        self._usage_by_user: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0, "cost_cents_f": 0.0}
        )
        # 全局锁（用于 snapshot_and_reset）
        self._global_lock = asyncio.Lock()

    def _get_shard_lock(self, user_id: str) -> asyncio.Lock:
        """根据 user_id 获取对应的分片锁"""
        shard_index = hash(user_id) % self.SHARD_COUNT
        return self._locks[shard_index]

    async def add(
        self, 
        user_id: str, 
        input_tokens: int = 0, 
        output_tokens: int = 0, 
        requests: int = 0
    ) -> None:
        """添加使用量（高性能，不阻塞请求）
        
        性能：< 1ms，锁内操作 < 100μs
        """
        if input_tokens == 0 and output_tokens == 0 and requests == 0:
            return
        
        # 计算在锁外进行，减少锁持有时间
        cost_cents = calculate_cost_cents(input_tokens, output_tokens)
        day = date.today()
        
        # 使用分片锁，减少竞争
        lock = self._get_shard_lock(user_id)
        async with lock:
            # 每日统计
            bucket = self._daily[(user_id, day)]
            bucket["input_tokens"] += int(input_tokens)
            bucket["output_tokens"] += int(output_tokens)
            bucket["requests"] += int(requests)
            bucket["cost_cents_f"] += cost_cents  # 保留浮点精度
            
            # 用户累计
            user_bucket = self._usage_by_user[user_id]
            user_bucket["input_tokens"] += int(input_tokens)
            user_bucket["output_tokens"] += int(output_tokens)
            user_bucket["cost_cents_f"] += cost_cents  # 保留浮点精度

    async def get_pending_tokens(self, user_id: str) -> int:
        """获取待刷新的总 tokens（兼容旧接口）
        
        注：无锁读取，Python dict.get() 是原子操作
        """
        usage = self._usage_by_user.get(user_id, {})
        return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

    async def get_pending_cost(self, user_id: str) -> int:
        """获取待刷新的费用（分，向上取整）
        
        注：无锁读取，Python dict.get() 是原子操作
        """
        import math
        usage = self._usage_by_user.get(user_id, {})
        # 向上取整，确保不会漏扣
        return math.ceil(usage.get("cost_cents_f", 0.0))

    async def get_pending_requests_today(self, user_id: str) -> int:
        """获取今日待刷新的请求数
        
        注：无锁读取，Python dict.get() 是原子操作
        """
        today = date.today()
        bucket = self._daily.get((user_id, today))
        if not bucket:
            return 0
        return int(bucket.get("requests", 0))

    async def snapshot_and_reset(self):
        """快照并重置缓冲区
        
        使用全局锁保证原子性
        """
        async with self._global_lock:
            # 获取所有分片锁
            for lock in self._locks:
                await lock.acquire()
            try:
                daily = dict(self._daily)
                usage_by_user = dict(self._usage_by_user)
                self._daily.clear()
                self._usage_by_user.clear()
            finally:
                # 释放所有分片锁
                for lock in self._locks:
                    lock.release()
        return daily, usage_by_user

    async def requeue(self, daily, usage_by_user) -> None:
        """重新入队（刷新失败时）
        
        使用全局锁保证原子性
        """
        async with self._global_lock:
            for lock in self._locks:
                await lock.acquire()
            try:
                for key, stats in daily.items():
                    bucket = self._daily[key]
                    bucket["input_tokens"] += int(stats.get("input_tokens", 0))
                    bucket["output_tokens"] += int(stats.get("output_tokens", 0))
                    bucket["requests"] += int(stats.get("requests", 0))
                    bucket["cost_cents_f"] += float(stats.get("cost_cents_f", 0))
                for user_id, usage in usage_by_user.items():
                    user_bucket = self._usage_by_user[user_id]
                    user_bucket["input_tokens"] += int(usage.get("input_tokens", 0))
                    user_bucket["output_tokens"] += int(usage.get("output_tokens", 0))
                    user_bucket["cost_cents_f"] += float(usage.get("cost_cents_f", 0))
            finally:
                for lock in self._locks:
                    lock.release()


usage_buffer = UsageBuffer()


async def flush_once() -> None:
    """将缓冲区数据刷新到数据库"""
    import math
    
    daily, usage_by_user = await usage_buffer.snapshot_and_reset()
    if not daily and not usage_by_user:
        return

    try:
        async with SessionLocal() as session:
            # 更新用户累计数据
            for user_id, usage in usage_by_user.items():
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                # 向上取整，确保扣费准确
                cost_cents = math.ceil(usage.get("cost_cents_f", 0.0))
                total_tokens = input_tokens + output_tokens
                
                if total_tokens > 0 or cost_cents > 0:
                    await session.execute(
                        update(User)
                        .where(User.id == user_id)
                        .values(
                            token_used=User.token_used + total_tokens,
                            input_tokens_used=User.input_tokens_used + input_tokens,
                            output_tokens_used=User.output_tokens_used + output_tokens,
                            balance=User.balance - cost_cents,  # 扣除余额
                        )
                    )

            # 更新每日统计
            for (user_id, day), stats in daily.items():
                input_tokens = int(stats.get("input_tokens", 0))
                output_tokens = int(stats.get("output_tokens", 0))
                requests = int(stats.get("requests", 0))
                # 向上取整
                cost_cents = math.ceil(stats.get("cost_cents_f", 0.0))
                total_tokens = input_tokens + output_tokens
                
                if total_tokens == 0 and requests == 0:
                    continue
                    
                stmt = mysql_insert(UsageDaily).values(
                    user_id=user_id,
                    day=day,
                    tokens_total=total_tokens,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_cents=cost_cents,
                    requests_total=requests,
                    updated_at=datetime.utcnow(),
                )
                stmt = stmt.on_duplicate_key_update(
                    tokens_total=UsageDaily.tokens_total + total_tokens,
                    input_tokens=UsageDaily.input_tokens + input_tokens,
                    output_tokens=UsageDaily.output_tokens + output_tokens,
                    cost_cents=UsageDaily.cost_cents + cost_cents,
                    requests_total=UsageDaily.requests_total + requests,
                    updated_at=datetime.utcnow(),
                )
                await session.execute(stmt)

            await session.commit()
    except Exception:
        logger.exception("usage flush failed; re-queueing")
        await usage_buffer.requeue(daily, usage_by_user)


async def flush_loop(interval_seconds: int) -> None:
    """定期刷新循环"""
    while True:
        await asyncio.sleep(interval_seconds)
        await flush_once()
