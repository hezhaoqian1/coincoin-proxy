"""Read-only request usage analysis. Billing and persistence remain unchanged."""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import RequestLog as Log, User
from .security import require_admin

router = APIRouter(prefix="/usage", dependencies=[Depends(require_admin)])
PERIODS = {"today": "今天", "24h": "近 24 小时", "yesterday": "昨天", "3d": "近 3 天",
           "this_week": "本周", "7d": "近 7 天", "30d": "近 30 天", "this_month": "本月", "custom": "自定义"}
EXPORT_LIMIT = 10000


def period_bounds(period, start_date=None, end_date=None, *, now=None):
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    local = now + timedelta(hours=8)
    midnight = datetime.combine(local.date(), datetime.min.time())
    end = now
    if period == "custom":
        try:
            start = datetime.combine(date.fromisoformat(start_date or ""), datetime.min.time()) - timedelta(hours=8)
            end = datetime.combine(date.fromisoformat(end_date or ""), datetime.min.time()) + timedelta(days=1, hours=-8)
        except (ValueError, OverflowError):
            raise HTTPException(400, "请填写有效的起止日期")
        if start > now:
            raise HTTPException(400, "自定义范围不能晚于当前时间")
        if start >= end or end - start > timedelta(days=93):
            raise HTTPException(400, "起止日期须正序，单次查询不超过 93 天")
        # A date range ending today is only populated up to the snapshot time.
        end = min(end, now)
    elif period == "today":
        start = midnight - timedelta(hours=8)
    elif period == "yesterday":
        end = midnight - timedelta(hours=8)
        start = end - timedelta(days=1)
    elif period in {"24h", "3d", "7d", "30d"}:
        start = now - timedelta(days={"24h": 1, "3d": 3, "7d": 7, "30d": 30}[period])
    elif period == "this_week":
        start = midnight - timedelta(days=local.weekday(), hours=8)
    elif period == "this_month":
        start = midnight.replace(day=1) - timedelta(hours=8)
    else:
        raise HTTPException(400, "不支持的时间范围")
    return start, end


def public_model():
    return func.coalesce(func.nullif(Log.customer_model_alias, ""), func.nullif(Log.model, ""), "未记录模型")


def model_filter_expr(value: str):
    """Match the model a customer saw as well as the routing/billing identifiers."""
    value = value.strip()
    return or_(
        Log.customer_model_alias.contains(value, autoescape=True),
        Log.model.contains(value, autoescape=True),
        Log.provider_model.contains(value, autoescape=True),
        Log.billable_sku.contains(value, autoescape=True),
    )


def charge_expr():
    # Existing admin billing convention, including signed video refunds.
    return case((func.coalesce(Log.retail_charge_cents, 0) != 0, Log.retail_charge_cents),
                else_=func.coalesce(Log.cost_cents, 0))


def refund_expr():
    return or_(func.coalesce(Log.route_reason, "") == "video_job_refund", charge_expr() < 0)


def cache_read_expr():
    return func.coalesce(func.nullif(Log.cache_read_tokens, 0), Log.cached_tokens, 0)


class UsageFilter:
    def __init__(self, period: str = "today", start_date: Optional[str] = None, end_date: Optional[str] = None,
                 user_id: str = "", search: Annotated[str, Query(max_length=128)] = "",
                 model: Annotated[str, Query(max_length=128)] = "", channel_id: Annotated[str, Query(max_length=32)] = "",
                 endpoint: Annotated[str, Query(max_length=64)] = "", api_key_id: Annotated[str, Query(max_length=32)] = "",
                 model_exact: Annotated[str, Query(max_length=128)] = "", as_of: Optional[datetime] = None,
                 result: Literal["all", "success", "failed", "refund"] = "all"):
        self.period = period
        now = datetime.now(timezone.utc)
        if as_of is not None:
            if as_of.tzinfo is None:
                raise HTTPException(400, "查询时间须包含时区")
            if as_of > now:
                raise HTTPException(400, "查询时间不能晚于当前时间")
            now = as_of
        self.as_of = now.isoformat()
        self.start, self.end = period_bounds(period, start_date, end_date, now=now)
        self.conditions = [Log.created_at >= self.start, Log.created_at < self.end]
        for column, value in ((Log.user_id, user_id), (Log.channel_id, channel_id),
                              (Log.endpoint, endpoint), (Log.api_key_id, api_key_id)):
            if value:
                self.conditions.append(column == value.strip())
        if model.strip():
            self.conditions.append(model_filter_expr(model))
        if model_exact.strip():
            self.conditions.append(public_model() == model_exact.strip())
        if search.strip():
            # A subquery keeps aggregation cardinality stable; wildcard input is literal.
            matching_users = select(User.id).where(or_(
                User.username.contains(search.strip(), autoescape=True),
                User.email.contains(search.strip(), autoescape=True),
                User.external_id.contains(search.strip(), autoescape=True),
                User.id.contains(search.strip(), autoescape=True)))
            self.conditions.append(Log.user_id.in_(matching_users))
        if result == "refund":
            self.conditions.append(refund_expr())
        elif result == "failed":
            self.conditions.extend([~refund_expr(), Log.status_code >= 400])
        elif result == "success":
            self.conditions.extend([~refund_expr(), Log.status_code >= 200, Log.status_code < 400])

    def metadata(self):
        return {"period": self.period, "period_label": PERIODS[self.period], "timezone": "Asia/Shanghai", "as_of": self.as_of,
                "window_start": self.start.replace(tzinfo=timezone.utc).isoformat(),
                "window_end": self.end.replace(tzinfo=timezone.utc).isoformat()}


def aggregate_columns():
    refund = refund_expr()
    request = ~refund
    def sum_(value, name):
        return func.coalesce(func.sum(value), 0).label(name)
    return [func.count(Log.id).label("records"), sum_(case((request, 1), else_=0), "requests"),
            func.count(func.distinct(Log.user_id)).label("users"),
            func.count(func.distinct(public_model())).label("models"),
            sum_(Log.input_tokens, "input_tokens"), sum_(Log.output_tokens, "output_tokens"),
            sum_(func.coalesce(Log.input_tokens, 0) + func.coalesce(Log.output_tokens, 0), "tokens"),
            sum_(cache_read_expr(), "cache_read_tokens"), sum_(Log.cache_creation_tokens, "cache_creation_tokens"),
            sum_(Log.image_count, "images"), sum_(Log.video_count, "videos"),
            sum_(charge_expr(), "cost_cents"),
            sum_(case((charge_expr() < 0, -charge_expr()), else_=0), "refund_cents"),
            sum_(case((refund, 1), else_=0), "refund_records"),
            sum_(case((request & (Log.status_code >= 400), 1), else_=0), "failed"),
            sum_(case((request & (Log.status_code >= 200) & (Log.status_code < 400), 1), else_=0), "succeeded"),
            sum_(case((request & (Log.route_attempt > 0), 1), else_=0), "fallback_requests"),
            func.avg(case((request & (Log.duration_ms > 0), Log.duration_ms), else_=None)).label("avg_latency_ms")]


def metrics(row):
    result = dict(row)
    for key in ("records", "requests", "users", "models", "input_tokens", "output_tokens", "tokens", "cache_read_tokens",
                "cache_creation_tokens", "images", "videos", "cost_cents", "refund_cents", "refund_records", "failed", "succeeded", "fallback_requests"):
        result[key] = int(result.get(key) or 0)
    result["avg_latency_ms"] = round(float(result["avg_latency_ms"])) if result.get("avg_latency_ms") is not None else None
    result["success_rate"] = result["succeeded"] / result["requests"] if result["requests"] else None
    result["failure_rate"] = result["failed"] / result["requests"] if result["requests"] else None
    result["cache_read_rate"] = result["cache_read_tokens"] / result["input_tokens"] if result["input_tokens"] else None
    return result


def filtered_query(filters, *columns):
    return select(*columns).select_from(Log).where(*filters.conditions)


@router.get("/overview")
async def overview(filters: UsageFilter = Depends(), db: AsyncSession = Depends(get_db)):
    summary = metrics((await db.execute(filtered_query(filters, *aggregate_columns()))).mappings().one())
    hourly = filters.end - filters.start <= timedelta(days=2)
    fmt = "%Y-%m-%d %H:00" if hourly else "%Y-%m-%d"
    bucket = func.date_format(func.convert_tz(Log.created_at, "+00:00", "+08:00"), fmt)
    # Trend only needs these three series. Avoid per-bucket DISTINCT users/models
    # and the remaining billing/latency aggregates used by the summary.
    series = [func.coalesce(func.sum(charge_expr()), 0).label("cost_cents"),
              func.coalesce(func.sum(func.coalesce(Log.input_tokens, 0) + func.coalesce(Log.output_tokens, 0)), 0).label("tokens"),
              func.coalesce(func.sum(case((~refund_expr(), 1), else_=0)), 0).label("requests")]
    rows = (await db.execute(filtered_query(filters, bucket.label("bucket"), *series)
                            .group_by(bucket).order_by(bucket))).mappings().all()
    by_bucket = {r["bucket"]: {name: int(r[name]) for name in ("cost_cents", "tokens", "requests")} for r in rows}
    cursor = filters.start + timedelta(hours=8)
    cursor = cursor.replace(minute=0, second=0, microsecond=0) if hourly else cursor.replace(hour=0, minute=0, second=0, microsecond=0)
    trend = []
    while cursor < filters.end + timedelta(hours=8):
        key = cursor.strftime(fmt)
        trend.append({"bucket": key, "cost_cents": 0, "tokens": 0, "requests": 0, **by_bucket.get(key, {})})
        cursor += timedelta(hours=1) if hourly else timedelta(days=1)
    return {**filters.metadata(), "summary": summary, "trend": trend, "granularity": "hour" if hourly else "day"}


@router.get("/groups")
async def groups(dimension: Literal["users", "models"] = "users", metric: Literal["cost_cents", "tokens", "requests", "images", "videos"] = "cost_cents",
                 offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                 include_total: bool = True,
                 filters: UsageFilter = Depends(), db: AsyncSession = Depends(get_db)):
    key = Log.user_id if dimension == "users" else public_model()
    aggregate = filtered_query(filters, key.label("key"), *aggregate_columns()).group_by(key).subquery()
    # Counting groups must not re-run every SUM/AVG in the ranking subquery.
    total = int((await db.scalar(filtered_query(filters, func.count(func.distinct(key))))) or 0) if include_total else None
    query = select(aggregate)
    if dimension == "users":
        query = query.add_columns(User.username, User.email, User.external_id).outerjoin(User, User.id == aggregate.c.key)
    rows = (await db.execute(query.order_by(aggregate.c[metric].desc(), aggregate.c.key.asc()).offset(offset).limit(limit))).mappings().all()
    items = []
    for index, row in enumerate(rows):
        item = metrics(row)
        item["rank"] = offset + index + 1
        item["display_name"] = (item.get("username") or item.get("email") or item.get("external_id") or item["key"])
        items.append(item)
    return {**filters.metadata(), "data": items, "total": total, "offset": offset, "limit": limit}


@router.get("/users/{user_id}/models")
async def user_models(user_id: str, filters: UsageFilter = Depends(), db: AsyncSession = Depends(get_db)):
    key = public_model()
    # Keep this legacy drill-down endpoint scoped to the requested user.  The
    # groups endpoint already supports the same combination of filters, but
    # callers of this explicit URL expect user_id to be authoritative.
    scoped = select(key.label("key"), *aggregate_columns()).select_from(Log).where(
        *filters.conditions, Log.user_id == user_id
    )
    rows = (await db.execute(scoped
                             .group_by(key).order_by(func.sum(charge_expr()).desc()))).mappings().all()
    return {**filters.metadata(), "user_id": user_id, "data": [
        {**metrics(row), "model": row.get("key") or "未记录模型"} for row in rows
    ]}


def record_query(filters):
    return select(Log, User.username, User.email, User.external_id).outerjoin(User, Log.user_id == User.id).where(*filters.conditions)


def serialize_record(row):
    log, username, email, external_id = row
    fields = ("id", "user_id", "api_key_id", "endpoint", "provider_model", "billable_sku", "input_tokens", "output_tokens",
              "cache_creation_tokens", "image_count", "video_count", "usage_unit_type", "usage_unit_count", "duration_ms", "status_code",
              "channel_id", "channel_type", "provider_platform", "route_attempt", "route_reason", "fallback_from_channel_id", "upstream_request_id",
              "station_id", "price_version", "pricing_mode", "user_billing_multiplier", "model_multiplier", "cache_read_multiplier", "output_multiplier", "image_multiplier", "video_multiplier",
              "base_price_input_per_million", "base_price_output_per_million", "base_price_per_image_cents",
              "price_per_video_cents", "effective_cached_input_per_million", "effective_cache_creation_input_per_million")
    # Historical schemas may not have every optional pricing snapshot field.
    item = {field: getattr(log, field, None) for field in fields}
    item.update(user=username or email or external_id or log.user_id,
                model=log.customer_model_alias or log.model or "未记录模型",
                cache_read_tokens=log.cache_read_tokens or log.cached_tokens or 0,
                cost_cents=log.retail_charge_cents or log.cost_cents or 0,
                created_at=log.created_at.replace(tzinfo=timezone.utc).isoformat())
    item["tokens"] = int(item.get("input_tokens") or 0) + int(item.get("output_tokens") or 0)
    item["is_refund"] = item["cost_cents"] < 0 or log.route_reason == "video_job_refund"
    return item


@router.get("/records")
async def records(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                  include_total: bool = True,
                  filters: UsageFilter = Depends(), db: AsyncSession = Depends(get_db)):
    total = int((await db.scalar(filtered_query(filters, func.count(Log.id)))) or 0) if include_total else None
    rows = (await db.execute(record_query(filters).order_by(Log.created_at.desc(), Log.id.desc()).offset(offset).limit(limit))).all()
    return {**filters.metadata(), "data": [serialize_record(row) for row in rows], "total": total, "offset": offset, "limit": limit}


@router.get("/export.csv")
async def export_records(filters: UsageFilter = Depends(), db: AsyncSession = Depends(get_db)):
    # Bounded export; never silently truncate records or hold a streaming DB session.
    rows = (await db.execute(record_query(filters).order_by(Log.created_at.desc(), Log.id.desc()).limit(EXPORT_LIMIT + 1))).all()
    if len(rows) > EXPORT_LIMIT:
        raise HTTPException(413, "当前筛选超过 10,000 条，请缩小时间范围或选择用户后导出")
    # CSV formatting is CPU work; a large export must not block the gateway's
    # event loop. All ORM columns are already loaded; no DB access in the worker.
    content = await run_in_threadpool(render_csv, rows)
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="coincoin-admin-usage.csv"'})


def render_csv(rows):
    columns = [("created_at", "请求时间 UTC"), ("id", "日志 ID"), ("user", "用户"), ("user_id", "用户 ID"),
               ("api_key_id", "Key ID"), ("model", "客户模型"), ("provider_model", "上游模型"),
               ("endpoint", "接口"), ("status_code", "状态码"), ("is_refund", "退款记录"),
               ("input_tokens", "输入 Token"), ("output_tokens", "输出 Token"), ("cache_read_tokens", "缓存读 Token"),
               ("cache_creation_tokens", "缓存写 Token"), ("image_count", "图片净数"), ("video_count", "视频净数"),
               ("cost_cents", "日志消耗 美分"), ("duration_ms", "耗时 ms"), ("channel_id", "渠道 ID"),
               ("provider_platform", "供应商"), ("route_attempt", "重试次数"), ("upstream_request_id", "上游请求 ID"),
               ("billable_sku", "SKU"), ("price_version", "价格版本"), ("user_billing_multiplier", "用户倍率"),
               ("tokens", "总 Token"), ("usage_unit_type", "用量单位"), ("usage_unit_count", "用量单位数量"),
               ("channel_type", "渠道类型"), ("route_reason", "路由原因"),
               ("fallback_from_channel_id", "Fallback 来源渠道"), ("station_id", "站点 ID"),
               ("pricing_mode", "计费模式"), ("model_multiplier", "模型倍率"), ("output_multiplier", "输出倍率"),
               ("cache_read_multiplier", "缓存读取倍率"), ("image_multiplier", "图片倍率"), ("video_multiplier", "视频倍率"),
               ("base_price_input_per_million", "基础输入价 美分/百万Token"),
               ("base_price_output_per_million", "基础输出价 美分/百万Token"),
               ("effective_cached_input_per_million", "有效缓存读取价 美分/百万Token"),
               ("effective_cache_creation_input_per_million", "有效缓存写入价 美分/百万Token"),
               ("base_price_per_image_cents", "基础图片价 美分/张"), ("price_per_video_cents", "视频价 美分/个")]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in columns])
    def safe(value):
        if not isinstance(value, str):
            return value
        return "'" + value if value.lstrip()[:1] in ("=", "+", "-", "@") or value.startswith(("\t", "\r", "\n")) else value
    for row in rows:
        item = serialize_record(row)
        writer.writerow([safe(item.get(key, "")) for key, _ in columns])
    return "\ufeff" + output.getvalue()
