import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import Announcement, ApiKey, PaymentOrder, RechargeLog, RedemptionCode, ReferralReward, RequestLog, UsageDaily, User
from .schemas import (
    AdminKeyUpdate, AdminUserUpdate,
    AnnouncementCreate, AnnouncementUpdate,
    RedemptionGenerateRequest, RedemptionGenerateResponse,
)
from .security import generate_api_key, generate_id, hash_key, require_admin


router = APIRouter(prefix="/admin", tags=["admin"])


def admin_guard(request: Request):
    require_admin(request)


@router.get("/ui")
async def admin_ui(token: str = ""):
    from .config import settings as _s
    if token != _s.admin_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    ui_path = Path(__file__).parent / "static" / "admin.html"
    return FileResponse(ui_path)


@router.get("/users", dependencies=[Depends(admin_guard)])
async def list_users(search: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = select(User).order_by(User.created_at.desc())
    if search:
        pat = f"%{search}%"
        query = query.where(
            User.username.ilike(pat)
            | User.external_id.ilike(pat)
            | User.id.ilike(pat)
        )
    result = await db.execute(query.limit(200))
    users = result.scalars().all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "external_id": u.external_id,
            "status": u.status,
            "balance": u.balance,
            "token_limit": u.token_limit,
            "token_used": u.token_used,
            "input_tokens_used": u.input_tokens_used,
            "output_tokens_used": u.output_tokens_used,
            "request_limit_per_minute": u.request_limit_per_minute,
            "request_limit_per_day": u.request_limit_per_day,
            "referral_code": u.referral_code,
            "referred_by": u.referred_by,
            "created_at": u.created_at,
            "updated_at": u.updated_at,
        }
        for u in users
    ]


@router.patch("/users/{user_id}", dependencies=[Depends(admin_guard)])
async def update_user(user_id: str, payload: AdminUserUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    if payload.status:
        user.status = payload.status
    if payload.balance is not None:
        user.balance = payload.balance
    if payload.token_limit is not None:
        user.token_limit = payload.token_limit
    if payload.token_used is not None:
        user.token_used = payload.token_used
    if payload.input_tokens_used is not None:
        user.input_tokens_used = payload.input_tokens_used
    if payload.output_tokens_used is not None:
        user.output_tokens_used = payload.output_tokens_used
    if payload.request_limit_per_minute is not None:
        user.request_limit_per_minute = payload.request_limit_per_minute
    if payload.request_limit_per_day is not None:
        user.request_limit_per_day = payload.request_limit_per_day

    await db.commit()
    return {
        "id": user.id,
        "status": user.status,
        "balance": user.balance,
        "token_limit": user.token_limit,
        "token_used": user.token_used,
        "input_tokens_used": user.input_tokens_used,
        "output_tokens_used": user.output_tokens_used,
        "request_limit_per_minute": user.request_limit_per_minute,
        "request_limit_per_day": user.request_limit_per_day,
    }


@router.post("/users/{user_id}/reset-usage", dependencies=[Depends(admin_guard)])
async def reset_user_usage(user_id: str, db: AsyncSession = Depends(get_db)):
    """重置用户的 token 使用量为 0"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    old_values = {
        "token_used": user.token_used,
        "input_tokens_used": user.input_tokens_used,
        "output_tokens_used": user.output_tokens_used,
    }
    user.token_used = 0
    user.input_tokens_used = 0
    user.output_tokens_used = 0
    await db.commit()
    
    return {
        "id": user.id,
        "before": old_values,
        "after": {
            "token_used": 0,
            "input_tokens_used": 0,
            "output_tokens_used": 0,
        },
        "message": "usage reset successfully"
    }


@router.get("/users/{user_id}", dependencies=[Depends(admin_guard)])
async def get_user_detail(user_id: str, db: AsyncSession = Depends(get_db)):
    """获取用户详情，包含该用户的所有 Key"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    
    # 获取用户的所有 Key
    keys_result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    keys = keys_result.scalars().all()
    
    return {
        "id": user.id,
        "username": user.username,
        "external_id": user.external_id,
        "status": user.status,
        "balance": user.balance,
        "balance_usd": user.balance / 100,  # 分转美元
        "token_limit": user.token_limit,
        "token_used": user.token_used,
        "input_tokens_used": user.input_tokens_used,
        "output_tokens_used": user.output_tokens_used,
        "request_limit_per_minute": user.request_limit_per_minute,
        "request_limit_per_day": user.request_limit_per_day,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "keys": [
            {
                "id": k.id,
                "status": k.status,
                "created_at": k.created_at,
                "last_used_at": k.last_used_at,
            }
            for k in keys
        ]
    }


@router.post("/users/{user_id}/keys", dependencies=[Depends(admin_guard)])
async def create_user_key(user_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    api_key_value = generate_api_key()
    key = ApiKey(
        id=generate_id("k_"),
        user_id=user.id,
        key_hash=hash_key(api_key_value),
        status="active",
        created_at=datetime.utcnow(),
    )
    db.add(key)
    await db.commit()

    return {"id": key.id, "api_key": api_key_value, "status": key.status}


@router.patch("/keys/{key_id}", dependencies=[Depends(admin_guard)])
async def update_key(key_id: str, payload: AdminKeyUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")

    if payload.status:
        key.status = payload.status

    await db.commit()
    return {"id": key.id, "status": key.status}


@router.get("/usage/daily", dependencies=[Depends(admin_guard)])
async def list_daily_usage(
    user_id: Optional[str] = None,
    day: Optional[date] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(UsageDaily, User).join(User, UsageDaily.user_id == User.id)
    if user_id:
        query = query.where(UsageDaily.user_id == user_id)
    if day:
        query = query.where(UsageDaily.day == day)
    result = await db.execute(query.order_by(UsageDaily.day.desc()).limit(200))
    rows = result.all()
    return [
        {
            "user_id": usage.user_id,
            "day": usage.day,
            "tokens_total": usage.tokens_total,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_cents": usage.cost_cents,
            "cost_usd": usage.cost_cents / 100,  # 分转美元
            "requests_total": usage.requests_total,
            "username": user.username,
            "external_id": user.external_id,
        }
        for usage, user in rows
    ]


@router.get("/metrics/summary", dependencies=[Depends(admin_guard)])
async def summary_metrics(db: AsyncSession = Depends(get_db)):
    total_users = await db.scalar(select(func.count()).select_from(User))
    active_users = await db.scalar(select(func.count()).select_from(User).where(User.status == "active"))
    total_tokens = await db.scalar(select(func.coalesce(func.sum(User.token_used), 0)))

    today = date.today()
    total_requests_today = await db.scalar(
        select(func.coalesce(func.sum(UsageDaily.requests_total), 0)).where(UsageDaily.day == today)
    )

    return {
        "total_users": int(total_users or 0),
        "active_users": int(active_users or 0),
        "total_tokens": int(total_tokens or 0),
        "total_requests_today": int(total_requests_today or 0),
    }


@router.get("/keys", dependencies=[Depends(admin_guard)])
async def list_keys(user_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """列出所有 Key"""
    query = select(ApiKey, User).join(User, ApiKey.user_id == User.id)
    if user_id:
        query = query.where(ApiKey.user_id == user_id)
    result = await db.execute(query.order_by(ApiKey.created_at.desc()).limit(200))
    rows = result.all()
    return [
        {
            "id": key.id,
            "user_id": key.user_id,
            "username": user.username,
            "external_id": user.external_id,
            "status": key.status,
            "created_at": key.created_at,
            "last_used_at": key.last_used_at,
        }
        for key, user in rows
    ]


@router.get("/recharges", dependencies=[Depends(admin_guard)])
async def list_recharges(user_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """列出充值记录"""
    query = select(RechargeLog, User).join(User, RechargeLog.user_id == User.id)
    if user_id:
        query = query.where(RechargeLog.user_id == user_id)
    result = await db.execute(query.order_by(RechargeLog.created_at.desc()).limit(200))
    rows = result.all()
    return [
        {
            "id": log.id,
            "order_id": log.order_id,
            "user_id": log.user_id,
            "username": user.username,
            "external_id": user.external_id,
            "amount": log.amount,
            "balance_added": log.balance_added,
            "balance_added_usd": log.balance_added / 100,  # 分转美元
            "tokens_added": log.tokens_added,
            "daily_requests_added": log.daily_requests_added,
            "note": log.note,
            "created_at": log.created_at,
        }
        for log, user in rows
    ]


@router.get("/users/{user_id}/request-logs", dependencies=[Depends(admin_guard)])
async def list_user_request_logs(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """查询用户的请求明细日志"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    count_result = await db.execute(
        select(func.count()).select_from(RequestLog).where(RequestLog.user_id == user_id)
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        select(RequestLog)
        .where(RequestLog.user_id == user_id)
        .order_by(RequestLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    logs = result.scalars().all()

    return {
        "user_id": user_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [
            {
                "created_at": (log.created_at.isoformat() + "Z") if log.created_at else None,
                "endpoint": log.endpoint,
                "model": log.model,
                "input_tokens": log.input_tokens,
                "output_tokens": log.output_tokens,
                "cached_tokens": getattr(log, "cached_tokens", 0),
                "total_tokens": log.input_tokens + log.output_tokens,
                "cost_cents": log.cost_cents,
                "cost_usd": log.cost_cents / 100,
                "duration_ms": log.duration_ms,
                "status_code": log.status_code,
                "route_reason": getattr(log, "route_reason", ""),
            }
            for log in logs
        ],
    }


# ============== Redemption Code Management ==============

def _generate_code() -> str:
    parts = [secrets.token_hex(2).upper() for _ in range(4)]
    return f"CC-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"


@router.post("/redemption-codes/generate", dependencies=[Depends(admin_guard)],
             response_model=RedemptionGenerateResponse)
async def generate_redemption_codes(
    payload: RedemptionGenerateRequest, db: AsyncSession = Depends(get_db)
):
    codes = []
    for _ in range(payload.count):
        code_str = _generate_code()
        code = RedemptionCode(
            id=generate_id("rc_"),
            code=code_str,
            balance_cents=payload.balance_cents,
            status="unused",
        )
        db.add(code)
        codes.append(code_str)
    await db.commit()
    return RedemptionGenerateResponse(
        codes=codes, balance_cents=payload.balance_cents, count=payload.count
    )


@router.get("/redemption-codes", dependencies=[Depends(admin_guard)])
async def list_redemption_codes(
    status_filter: Optional[str] = None, db: AsyncSession = Depends(get_db)
):
    query = select(RedemptionCode).order_by(RedemptionCode.created_at.desc())
    if status_filter:
        query = query.where(RedemptionCode.status == status_filter)
    result = await db.execute(query.limit(200))
    codes = result.scalars().all()
    return [
        {
            "id": c.id,
            "code": c.code,
            "balance_cents": c.balance_cents,
            "status": c.status,
            "used_by": c.used_by,
            "used_at": c.used_at,
            "created_at": c.created_at,
        }
        for c in codes
    ]


@router.patch("/redemption-codes/{code_id}", dependencies=[Depends(admin_guard)])
async def disable_redemption_code(code_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RedemptionCode).where(RedemptionCode.id == code_id))
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="code not found")
    code.status = "disabled"
    await db.commit()
    return {"id": code.id, "status": code.status}


# ============== Announcement Management ==============

@router.post("/announcements", dependencies=[Depends(admin_guard)])
async def create_announcement(payload: AnnouncementCreate, db: AsyncSession = Depends(get_db)):
    ann = Announcement(
        id=generate_id("ann_"),
        title=payload.title,
        content=payload.content,
        priority=payload.priority,
        status="active",
    )
    db.add(ann)
    await db.commit()
    return {"id": ann.id, "title": ann.title, "status": ann.status}


@router.get("/announcements", dependencies=[Depends(admin_guard)])
async def list_announcements_admin(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Announcement).order_by(Announcement.created_at.desc()).limit(50)
    )
    anns = result.scalars().all()
    return [
        {
            "id": a.id,
            "title": a.title,
            "content": a.content,
            "priority": a.priority,
            "status": a.status,
            "created_at": a.created_at,
        }
        for a in anns
    ]


@router.patch("/announcements/{ann_id}", dependencies=[Depends(admin_guard)])
async def update_announcement(
    ann_id: str, payload: AnnouncementUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Announcement).where(Announcement.id == ann_id))
    ann = result.scalar_one_or_none()
    if not ann:
        raise HTTPException(status_code=404, detail="announcement not found")
    if payload.title is not None:
        ann.title = payload.title
    if payload.content is not None:
        ann.content = payload.content
    if payload.priority is not None:
        ann.priority = payload.priority
    if payload.status is not None:
        ann.status = payload.status
    await db.commit()
    return {"id": ann.id, "title": ann.title, "status": ann.status}


# ============== Payment Order Management ==============

@router.get("/payment-orders", dependencies=[Depends(admin_guard)])
async def list_payment_orders(
    status_filter: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    query = select(PaymentOrder).order_by(PaymentOrder.created_at.desc())
    if status_filter:
        query = query.where(PaymentOrder.status == status_filter)
    result = await db.execute(query.limit(limit))
    orders = result.scalars().all()
    return [
        {
            "id": o.id,
            "user_id": o.user_id,
            "order_no": o.order_no,
            "amount_rmb": o.amount_rmb,
            "add_balance_cents": o.add_balance_cents,
            "status": o.status,
            "trade_no": o.trade_no,
            "pay_url": o.pay_url,
            "created_at": o.created_at,
            "confirmed_at": o.confirmed_at,
        }
        for o in orders
    ]


@router.post("/payment-orders/{order_no}/force-confirm", dependencies=[Depends(admin_guard)])
async def force_confirm_order(order_no: str, db: AsyncSession = Depends(get_db)):
    """Admin 手动补单：查询支付服务验证后强制入账。"""
    from .webhook import _do_confirm_order

    order = (
        await db.execute(select(PaymentOrder).where(PaymentOrder.order_no == order_no))
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status == "confirmed":
        return {"order_no": order_no, "status": "already_confirmed"}

    ok = await _do_confirm_order(order_no, db)
    if not ok:
        raise HTTPException(status_code=502, detail="payment verification failed or not paid yet")
    return {"order_no": order_no, "status": "confirmed"}


# ============== Referral Rewards ==============

@router.get("/referral-rewards", dependencies=[Depends(admin_guard)])
async def list_referral_rewards(
    referrer_id: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(ReferralReward, User)
        .join(User, ReferralReward.referrer_id == User.id)
        .order_by(ReferralReward.created_at.desc())
    )
    if referrer_id:
        query = query.where(ReferralReward.referrer_id == referrer_id)
    result = await db.execute(query.limit(limit))
    rows = result.all()
    return [
        {
            "id": r.id,
            "referrer_id": r.referrer_id,
            "referrer_username": u.username,
            "referred_id": r.referred_id,
            "order_no": r.order_no,
            "order_amount_cents": r.order_amount_cents,
            "reward_cents": r.reward_cents,
            "reward_usd": r.reward_cents / 100,
            "created_at": r.created_at,
        }
        for r, u in rows
    ]


@router.get("/referral-stats", dependencies=[Depends(admin_guard)])
async def referral_stats(db: AsyncSession = Depends(get_db)):
    total_rewards = await db.scalar(
        select(func.coalesce(func.sum(ReferralReward.reward_cents), 0))
    ) or 0
    total_referrals = await db.scalar(
        select(func.count()).select_from(User).where(User.referred_by.isnot(None))
    ) or 0
    total_referrers = await db.scalar(
        select(func.count(func.distinct(ReferralReward.referrer_id)))
    ) or 0
    return {
        "total_rewards_cents": total_rewards,
        "total_rewards_usd": total_rewards / 100,
        "total_referred_users": total_referrals,
        "total_active_referrers": total_referrers,
    }
