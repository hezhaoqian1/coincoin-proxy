from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import ApiKey, RechargeLog, UsageDaily, User
from .schemas import AdminKeyUpdate, AdminUserUpdate
from .security import generate_api_key, generate_id, hash_key, require_admin


router = APIRouter(prefix="/admin", tags=["admin"])


def admin_guard(request: Request):
    require_admin(request)


@router.get("/ui")
async def admin_ui():
    ui_path = Path(__file__).parent / "static" / "admin.html"
    return FileResponse(ui_path)


@router.get("/users", dependencies=[Depends(admin_guard)])
async def list_users(search: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = select(User)
    if search:
        query = query.where((User.username == search) | (User.external_id == search))
    result = await db.execute(query.limit(200))
    users = result.scalars().all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "external_id": u.external_id,
            "status": u.status,
            "token_limit": u.token_limit,
            "token_used": u.token_used,
            "request_limit_per_minute": u.request_limit_per_minute,
            "request_limit_per_day": u.request_limit_per_day,
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
    if payload.token_limit is not None:
        user.token_limit = payload.token_limit
    if payload.token_used is not None:
        user.token_used = payload.token_used
    if payload.request_limit_per_minute is not None:
        user.request_limit_per_minute = payload.request_limit_per_minute
    if payload.request_limit_per_day is not None:
        user.request_limit_per_day = payload.request_limit_per_day

    await db.commit()
    return {
        "id": user.id,
        "status": user.status,
        "token_limit": user.token_limit,
        "token_used": user.token_used,
        "request_limit_per_minute": user.request_limit_per_minute,
        "request_limit_per_day": user.request_limit_per_day,
    }


@router.post("/users/{user_id}/reset-usage", dependencies=[Depends(admin_guard)])
async def reset_user_usage(user_id: str, db: AsyncSession = Depends(get_db)):
    """重置用户的 token_used 为 0"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    old_value = user.token_used
    user.token_used = 0
    await db.commit()
    
    return {
        "id": user.id,
        "token_used_before": old_value,
        "token_used_after": 0,
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
        "token_limit": user.token_limit,
        "token_used": user.token_used,
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
            "tokens_added": log.tokens_added,
            "daily_requests_added": log.daily_requests_added,
            "note": log.note,
            "created_at": log.created_at,
        }
        for log, user in rows
    ]
