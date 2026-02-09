from datetime import datetime

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import ApiKey, User
from .schemas import KeyActivateRequest, KeyActivateResponse
from .security import generate_api_key, generate_id, hash_key


router = APIRouter(prefix="/v1/keys", tags=["keys"])
logger = logging.getLogger("coincoin.keys")


@router.post("/activate", response_model=KeyActivateResponse)
async def activate_key(payload: KeyActivateRequest, db: AsyncSession = Depends(get_db)):
    if not payload.username and not payload.external_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="username or external_id required")

    user = None
    try:
        if payload.username:
            result = await db.execute(select(User).where(User.username == payload.username))
            user = result.scalar_one_or_none()
        if not user and payload.external_id:
            result = await db.execute(select(User).where(User.external_id == payload.external_id))
            user = result.scalar_one_or_none()

        if not user:
            user = User(
                id=generate_id("u_"),
                username=payload.username,
                external_id=payload.external_id,
                status="active",
                token_used=0,
                balance=settings.default_balance,
            )
            db.add(user)
            await db.flush()
        else:
            if user.status != "active":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user blocked")
            if payload.username and not user.username:
                user.username = payload.username
            if payload.external_id and not user.external_id:
                user.external_id = payload.external_id

        api_key_value = generate_api_key()
        key = ApiKey(
            id=generate_id("k_"),
            user_id=user.id,
            key_hash=hash_key(api_key_value),
            status="active",
            last_used_at=None,
            created_at=datetime.utcnow(),
        )
        db.add(key)
        await db.commit()

        return KeyActivateResponse(user_id=user.id, api_key=api_key_value, status="active")
    except Exception:
        logger.exception("activate_key failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error")
