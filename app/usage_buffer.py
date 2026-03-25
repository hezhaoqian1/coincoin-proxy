import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from .config import settings
from .db import SessionLocal
from .models import RequestLog, UsageDaily, User
from .security import generate_id


logger = logging.getLogger("coincoin.usage")


def extract_cached_tokens(usage: dict) -> int:
    """Extract cached_tokens across Chat Completions and Responses API shapes."""
    if not isinstance(usage, dict):
        return 0
    details = usage.get("input_tokens_details") or {}
    ct = details.get("cached_tokens")
    if ct is not None:
        try:
            return int(ct)
        except (TypeError, ValueError):
            return 0
    details = usage.get("prompt_tokens_details") or {}
    ct = details.get("cached_tokens")
    if ct is not None:
        try:
            return int(ct)
        except (TypeError, ValueError):
            return 0
    return 0


def calculate_cost_cents(
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    price_input_per_million: int = 0,
    price_output_per_million: int = 0,
) -> float:
    """计算消费金额（单位：分，保留小数精度）
    
    价格配置单位是 分/百万tokens
    计算公式: (tokens / 1_000_000) * price_per_million
    
    注：返回浮点数，保留精度以避免小请求被舍入到 0
    最终在 flush 时累积后再取整
    """
    price_in = int(price_input_per_million or settings.price_input_per_million)
    price_out = int(price_output_per_million or settings.price_output_per_million)

    try:
        # charge_ratio: 0.5 = cached tokens 按 50% 价格收费（即打五折），0.0 = 全免
        discount = float(settings.cache_discount_rate)
    except Exception:
        discount = 0.5
    if discount < 0.0:
        discount = 0.0
    if discount > 1.0:
        discount = 1.0

    ct = int(cached_tokens or 0)
    if ct < 0:
        ct = 0
    it = int(input_tokens or 0)
    if ct > it:
        ct = it

    non_cached = max(0, it - ct)
    cached_price_in = price_in * discount
    input_cost = (non_cached * price_in + ct * cached_price_in) / 1_000_000
    output_cost = (int(output_tokens or 0) * price_out) / 1_000_000
    return input_cost + output_cost


def calculate_image_cost_cents(image_count: int, price_per_image_cents: int = 0) -> float:
    return int(image_count or 0) * int(price_per_image_cents or 0)


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
        # 每日统计: (user_id, date) -> {input_tokens, output_tokens, images_total, requests, cost_cents_f}
        # cost_cents_f 使用 float 保留精度
        self._daily: Dict[Tuple[str, date], Dict[str, float]] = defaultdict(
            lambda: {
                "input_tokens": 0,
                "output_tokens": 0,
                "images_total": 0,
                "requests": 0,
                "cost_cents_f": 0.0,
            }
        )
        # 用户累计: user_id -> {input_tokens, output_tokens, images_total, cost_cents_f}
        self._usage_by_user: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {
                "input_tokens": 0,
                "output_tokens": 0,
                "images_total": 0,
                "cost_cents_f": 0.0,
            }
        )
        # 请求日志缓冲（每次 API 调用一条）
        self._request_logs: List[dict] = []
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
        cached_tokens: int = 0,
        requests: int = 0,
        endpoint: str = "",
        model: str = "",
        customer_model_alias: str = "",
        provider_model: str = "",
        route_reason: str = "",
        duration_ms: int = 0,
        status_code: int = 200,
        price_input_per_million: int = 0,
        price_output_per_million: int = 0,
        usage_unit_type: str = "tokens",
        usage_unit_count: int = 0,
        billable_sku: str = "",
        image_count: int = 0,
        cost_cents_override: Optional[float] = None,
        price_per_image_cents: int = 0,
    ) -> None:
        """添加使用量（高性能，不阻塞请求）
        
        性能：< 1ms，锁内操作 < 100μs
        """
        if (
            input_tokens == 0
            and output_tokens == 0
            and requests == 0
            and image_count == 0
            and usage_unit_count == 0
            and not cost_cents_override
        ):
            return
        
        # 计算在锁外进行，减少锁持有时间
        if cost_cents_override is None:
            if (usage_unit_type or "tokens") == "images":
                cost_cents = calculate_image_cost_cents(
                    image_count=image_count or usage_unit_count,
                    price_per_image_cents=price_per_image_cents,
                )
            else:
                cost_cents = calculate_cost_cents(
                    input_tokens,
                    output_tokens,
                    cached_tokens=cached_tokens,
                    price_input_per_million=price_input_per_million,
                    price_output_per_million=price_output_per_million,
                )
        else:
            cost_cents = float(cost_cents_override)

        resolved_usage_unit_type = (usage_unit_type or "tokens").strip() or "tokens"
        resolved_usage_unit_count = int(
            usage_unit_count
            or (image_count if resolved_usage_unit_type == "images" else (input_tokens + output_tokens))
        )
        day = date.today()
        
        # 使用分片锁，减少竞争
        lock = self._get_shard_lock(user_id)
        async with lock:
            # 每日统计
            bucket = self._daily[(user_id, day)]
            bucket["input_tokens"] += int(input_tokens)
            bucket["output_tokens"] += int(output_tokens)
            bucket["images_total"] += int(image_count or 0)
            bucket["requests"] += int(requests)
            bucket["cost_cents_f"] += cost_cents  # 保留浮点精度
            
            # 用户累计
            user_bucket = self._usage_by_user[user_id]
            user_bucket["input_tokens"] += int(input_tokens)
            user_bucket["output_tokens"] += int(output_tokens)
            user_bucket["images_total"] += int(image_count or 0)
            user_bucket["cost_cents_f"] += cost_cents  # 保留浮点精度
            
            # 请求日志（append 到 list，纳秒级）
            self._request_logs.append({
                "user_id": user_id,
                "endpoint": endpoint,
                "model": customer_model_alias or model,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "cached_tokens": int(cached_tokens or 0),
                "image_count": int(image_count or 0),
                "provider_model": (provider_model or model)[:128],
                "customer_model_alias": (customer_model_alias or model)[:128],
                "usage_unit_type": resolved_usage_unit_type[:32],
                "usage_unit_count": resolved_usage_unit_count,
                "billable_sku": (billable_sku or model)[:128],
                "cost_cents": round(cost_cents),
                "duration_ms": int(duration_ms),
                "status_code": int(status_code),
                "route_reason": (route_reason or "")[:64],
                "created_at": datetime.utcnow(),
            })

    async def get_pending_tokens(self, user_id: str) -> int:
        """获取待刷新的总 tokens（兼容旧接口）
        
        注：无锁读取，Python dict.get() 是原子操作
        """
        usage = self._usage_by_user.get(user_id, {})
        return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

    async def get_pending_cost(self, user_id: str) -> int:
        """获取待刷新的费用（分，四舍五入）
        
        注：无锁读取，Python dict.get() 是原子操作
        """
        usage = self._usage_by_user.get(user_id, {})
        return round(usage.get("cost_cents_f", 0.0))

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
                request_logs = list(self._request_logs)
                self._daily.clear()
                self._usage_by_user.clear()
                self._request_logs.clear()
            finally:
                # 释放所有分片锁
                for lock in self._locks:
                    lock.release()
        return daily, usage_by_user, request_logs

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
                    bucket["images_total"] += int(stats.get("images_total", 0))
                    bucket["requests"] += int(stats.get("requests", 0))
                    bucket["cost_cents_f"] += float(stats.get("cost_cents_f", 0))
                for user_id, usage in usage_by_user.items():
                    user_bucket = self._usage_by_user[user_id]
                    user_bucket["input_tokens"] += int(usage.get("input_tokens", 0))
                    user_bucket["output_tokens"] += int(usage.get("output_tokens", 0))
                    user_bucket["images_total"] += int(usage.get("images_total", 0))
                    user_bucket["cost_cents_f"] += float(usage.get("cost_cents_f", 0))
            finally:
                for lock in self._locks:
                    lock.release()


usage_buffer = UsageBuffer()


async def flush_once() -> None:
    """将缓冲区数据刷新到数据库"""
    daily, usage_by_user, request_logs = await usage_buffer.snapshot_and_reset()
    if not daily and not usage_by_user and not request_logs:
        return

    try:
        async with SessionLocal() as session:
            # 更新用户累计数据
            for user_id, usage in usage_by_user.items():
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                cost_cents = round(usage.get("cost_cents_f", 0.0))
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
                images_total = int(stats.get("images_total", 0))
                requests = int(stats.get("requests", 0))
                cost_cents = round(stats.get("cost_cents_f", 0.0))
                total_tokens = input_tokens + output_tokens
                
                if total_tokens == 0 and images_total == 0 and requests == 0:
                    continue
                    
                stmt = mysql_insert(UsageDaily).values(
                    user_id=user_id,
                    day=day,
                    tokens_total=total_tokens,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    images_total=images_total,
                    cost_cents=cost_cents,
                    requests_total=requests,
                    updated_at=datetime.utcnow(),
                )
                stmt = stmt.on_duplicate_key_update(
                    tokens_total=UsageDaily.tokens_total + total_tokens,
                    input_tokens=UsageDaily.input_tokens + input_tokens,
                    output_tokens=UsageDaily.output_tokens + output_tokens,
                    images_total=UsageDaily.images_total + images_total,
                    cost_cents=UsageDaily.cost_cents + cost_cents,
                    requests_total=UsageDaily.requests_total + requests,
                    updated_at=datetime.utcnow(),
                )
                await session.execute(stmt)

            # 批量插入请求日志
            if request_logs:
                session.add_all([
                    RequestLog(
                        id=generate_id("rl_"),
                        user_id=log["user_id"],
                        endpoint=log["endpoint"],
                        model=log["model"],
                        input_tokens=log["input_tokens"],
                        output_tokens=log["output_tokens"],
                        cached_tokens=log.get("cached_tokens", 0),
                        image_count=log.get("image_count", 0),
                        provider_model=log.get("provider_model", ""),
                        customer_model_alias=log.get("customer_model_alias", ""),
                        usage_unit_type=log.get("usage_unit_type", "tokens"),
                        usage_unit_count=log.get("usage_unit_count", 0),
                        billable_sku=log.get("billable_sku", ""),
                        cost_cents=log["cost_cents"],
                        duration_ms=log["duration_ms"],
                        status_code=log["status_code"],
                        route_reason=log.get("route_reason", ""),
                        created_at=log["created_at"],
                    )
                    for log in request_logs
                ])

            await session.commit()
    except Exception:
        logger.exception("usage flush failed; re-queueing")
        await usage_buffer.requeue(daily, usage_by_user)
        # 请求日志丢失可接受，不 requeue（避免无限重试）


async def flush_loop(interval_seconds: int) -> None:
    """定期刷新循环"""
    while True:
        await asyncio.sleep(interval_seconds)
        await flush_once()
