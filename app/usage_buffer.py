import asyncio
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from .config import settings
from .db import SessionLocal
from .finance_summary import increment_finance_summary
from .billing import debit_usage_cents
from .models import RequestLog, UsageDaily, User
from .referral import process_first_usage_referral_reward
from .security import generate_id


logger = logging.getLogger("coincoin.usage")
CHINA_TZ_OFFSET = timedelta(hours=8)


def china_today(now: datetime | None = None) -> date:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current.astimezone(UTC) + CHINA_TZ_OFFSET).date()


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def extract_cache_read_tokens(usage: dict) -> int:
    """Extract cache-read tokens across Anthropic, Chat Completions, and Responses shapes."""
    if not isinstance(usage, dict):
        return 0
    ct = usage.get("cache_read_input_tokens")
    if ct is not None:
        return max(0, _safe_int(ct))
    details = usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    ct = details.get("cached_tokens")
    if ct is not None:
        return max(0, _safe_int(ct))
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    ct = details.get("cached_tokens")
    if ct is not None:
        return max(0, _safe_int(ct))
    return 0


def extract_cache_creation_tokens(usage: dict) -> int:
    """Extract Anthropic cache-write tokens when the upstream reports them."""
    if not isinstance(usage, dict):
        return 0
    total = _safe_int(usage.get("cache_creation_input_tokens"))
    if total:
        return max(0, total)
    cache_creation = usage.get("cache_creation") or {}
    if isinstance(cache_creation, dict):
        total += _safe_int(cache_creation.get("ephemeral_5m_input_tokens"))
        total += _safe_int(cache_creation.get("ephemeral_1h_input_tokens"))
    return max(0, total)


def extract_total_input_tokens(usage: dict) -> int:
    """Extract total input tokens while preserving provider-specific cache semantics."""
    if not isinstance(usage, dict):
        return 0
    if usage.get("prompt_tokens") is not None:
        return max(0, _safe_int(usage.get("prompt_tokens")))
    input_tokens = max(0, _safe_int(usage.get("input_tokens")))
    if usage.get("cache_read_input_tokens") is not None or usage.get("cache_creation_input_tokens") is not None:
        return input_tokens + extract_cache_read_tokens(usage) + extract_cache_creation_tokens(usage)
    return input_tokens


def extract_cached_tokens(usage: dict) -> int:
    """Backward-compatible alias for cache-read tokens."""
    return extract_cache_read_tokens(usage)


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

    ct = max(0, int(cached_tokens or 0))
    it = int(input_tokens or 0)
    if ct > it:
        ct = it

    non_cached = max(0, it - ct)
    cached_price_in = price_in * discount
    input_cost = (non_cached * price_in + ct * cached_price_in) / 1_000_000
    output_cost = (int(output_tokens or 0) * price_out) / 1_000_000
    return input_cost + output_cost


def calculate_image_cost_cents(image_count: int, price_per_image_cents: float = 0.0) -> float:
    return int(image_count or 0) * float(price_per_image_cents or 0.0)


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
        self._cost_by_api_key: Dict[str, float] = defaultdict(float)
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
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        requests: int = 0,
        endpoint: str = "",
        model: str = "",
        api_key_id: str = "",
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
        upstream_request_id: str = "",
        image_count: int = 0,
        cost_cents_override: Optional[float] = None,
        price_per_image_cents: float = 0.0,
        station_id: str = "",
        station_alias: str = "",
        resolved_public_model: str = "",
        wholesale_price_input_per_million: int = 0,
        wholesale_price_output_per_million: int = 0,
        wholesale_price_per_image_cents: float = 0.0,
        wholesale_cost_cents_override: Optional[float] = None,
        retail_charge_cents_override: Optional[float] = None,
        price_version: int = 0,
    ) -> None:
        """添加使用量（高性能，不阻塞请求）
        
        性能：< 1ms，锁内操作 < 100μs
        """
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        cache_read_tokens = int(cache_read_tokens or 0)
        cached_tokens = int(cached_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        if input_tokens < cache_read_tokens + cache_creation_tokens:
            input_tokens += cache_read_tokens + cache_creation_tokens

        if (
            input_tokens == 0
            and output_tokens == 0
            and cached_tokens == 0
            and cache_read_tokens == 0
            and cache_creation_tokens == 0
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
                    cached_tokens=cache_read_tokens or cached_tokens,
                    price_input_per_million=price_input_per_million,
                    price_output_per_million=price_output_per_million,
                )
        else:
            cost_cents = float(cost_cents_override)

        if wholesale_cost_cents_override is not None:
            wholesale_cost_cents = float(wholesale_cost_cents_override)
        elif station_id:
            if (usage_unit_type or "tokens") == "images":
                wholesale_cost_cents = calculate_image_cost_cents(
                    image_count=image_count or usage_unit_count,
                    price_per_image_cents=wholesale_price_per_image_cents,
                )
            else:
                wholesale_cost_cents = calculate_cost_cents(
                    input_tokens,
                    output_tokens,
                    cached_tokens=cache_read_tokens or cached_tokens,
                    price_input_per_million=wholesale_price_input_per_million,
                    price_output_per_million=wholesale_price_output_per_million,
                )
        else:
            wholesale_cost_cents = 0.0

        retail_charge_cents = float(retail_charge_cents_override) if retail_charge_cents_override is not None else cost_cents

        resolved_usage_unit_type = (usage_unit_type or "tokens").strip() or "tokens"
        resolved_usage_unit_count = int(
            usage_unit_count
            or (image_count if resolved_usage_unit_type == "images" else (input_tokens + output_tokens))
        )
        day = china_today()
        
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
            if api_key_id:
                self._cost_by_api_key[(api_key_id or "")[:32]] += cost_cents
            
            # 请求日志（append 到 list，纳秒级）
            self._request_logs.append({
                "user_id": user_id,
                "api_key_id": (api_key_id or "")[:32],
                "endpoint": endpoint,
                "model": customer_model_alias or model,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "cached_tokens": int(cache_read_tokens or cached_tokens or 0),
                "cache_read_tokens": int(cache_read_tokens or cached_tokens or 0),
                "cache_creation_tokens": int(cache_creation_tokens or 0),
                "image_count": int(image_count or 0),
                "provider_model": (provider_model or model)[:128],
                "customer_model_alias": (customer_model_alias or model)[:128],
                "usage_unit_type": resolved_usage_unit_type[:32],
                "usage_unit_count": resolved_usage_unit_count,
                "billable_sku": (billable_sku or model)[:128],
                "upstream_request_id": (upstream_request_id or "")[:128],
                "station_id": (station_id or "")[:32],
                "station_alias": (station_alias or "")[:128],
                "resolved_public_model": (resolved_public_model or "")[:128],
                "wholesale_cost_cents": round(wholesale_cost_cents),
                "retail_charge_cents": round(retail_charge_cents),
                "price_version": int(price_version or 0),
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

    async def get_pending_cost_for_api_key(self, api_key_id: str) -> int:
        if not api_key_id:
            return 0
        return round(self._cost_by_api_key.get(api_key_id[:32], 0.0))

    async def get_pending_requests_today(self, user_id: str) -> int:
        """获取今日待刷新的请求数
        
        注：无锁读取，Python dict.get() 是原子操作
        """
        today = china_today()
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
                self._cost_by_api_key.clear()
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
                    user = (
                        await session.execute(select(User).where(User.id == user_id).with_for_update())
                    ).scalar_one_or_none()
                    if not user:
                        continue
                    user.token_used = int(user.token_used or 0) + total_tokens
                    user.input_tokens_used = int(user.input_tokens_used or 0) + input_tokens
                    user.output_tokens_used = int(user.output_tokens_used or 0) + output_tokens
                    await debit_usage_cents(
                        db=session,
                        user=user,
                        cost_cents=cost_cents,
                        source_id=f"usage_flush:{china_today().isoformat()}",
                    )
                    await increment_finance_summary(
                        session,
                        user_id,
                        consumed_cents=cost_cents,
                    )
                    await process_first_usage_referral_reward(user_id, session)

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
                        api_key_id=log.get("api_key_id") or None,
                        endpoint=log["endpoint"],
                        model=log["model"],
                        input_tokens=log["input_tokens"],
                        output_tokens=log["output_tokens"],
                        cached_tokens=log.get("cached_tokens", 0),
                        cache_read_tokens=log.get("cache_read_tokens", log.get("cached_tokens", 0)),
                        cache_creation_tokens=log.get("cache_creation_tokens", 0),
                        image_count=log.get("image_count", 0),
                        provider_model=log.get("provider_model", ""),
                        customer_model_alias=log.get("customer_model_alias", ""),
                        usage_unit_type=log.get("usage_unit_type", "tokens"),
                        usage_unit_count=log.get("usage_unit_count", 0),
                        billable_sku=log.get("billable_sku", ""),
                        upstream_request_id=log.get("upstream_request_id", ""),
                        station_id=log.get("station_id", ""),
                        station_alias=log.get("station_alias", ""),
                        resolved_public_model=log.get("resolved_public_model", ""),
                        wholesale_cost_cents=log.get("wholesale_cost_cents", 0),
                        retail_charge_cents=log.get("retail_charge_cents", log["cost_cents"]),
                        price_version=log.get("price_version", 0),
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
