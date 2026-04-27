"""
Web authentication endpoints — register + login with username/password.
Session keys (kind='session') are issued for Dashboard access only;
they cannot be used on billing endpoints (chat/completions, responses).
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import Account, ApiKey, User
from .rate_limiter import rate_limiter
from .schemas import AuthLoginRequest, AuthRegisterRequest, AuthResponse
from .security import (
    encrypt_api_key,
    generate_api_key,
    generate_id,
    generate_referral_code,
    hash_key,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])
logger = logging.getLogger("coincoin.auth")

AUTH_RATE_LIMIT = 10  # per IP per minute
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
SESSION_KEY_DAYS = 7


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _create_session_key(user_id: str) -> tuple[str, ApiKey]:
    raw_key = generate_api_key()
    api_key = ApiKey(
        id=generate_id("k_"),
        user_id=user_id,
        key_hash=hash_key(raw_key),
        encrypted_key=encrypt_api_key(raw_key),
        kind="session",
        status="active",
        expires_at=datetime.utcnow() + timedelta(days=SESSION_KEY_DAYS),
        created_at=datetime.utcnow(),
    )
    return raw_key, api_key


@router.post("/register", response_model=AuthResponse)
async def register(
    payload: AuthRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_register:{ip}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    existing = (
        await db.execute(select(Account).where(Account.username == payload.username))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")

    referrer_id = None
    if payload.referral_code:
        referrer = (
            await db.execute(select(User).where(User.referral_code == payload.referral_code.strip().upper()))
        ).scalar_one_or_none()
        if referrer:
            if getattr(referrer, "register_ip", None) and referrer.register_ip == ip:
                logger.warning("referral blocked: same IP %s (referrer=%s)", ip, referrer.id)
            else:
                referrer_id = referrer.id

    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()

    if not user:
        user = User(
            id=generate_id("u_"),
            username=payload.username,
            status="active",
            token_used=0,
            balance=settings.default_balance,
            referral_code=generate_referral_code(),
            referred_by=referrer_id,
            register_ip=ip,
        )
        db.add(user)
        await db.flush()
        await ensure_finance_summary_initialized(db, user.id, commit=False)
        if settings.default_balance > 0:
            await increment_finance_summary(db, user.id, bonus_cents=settings.default_balance)

    account = Account(
        id=generate_id("acc_"),
        username=payload.username,
        password_hash=await hash_password(payload.password),
        linked_user_id=user.id,
    )
    db.add(account)

    raw_key, session_key = _create_session_key(user.id)
    db.add(session_key)

    await db.commit()
    logger.info("User registered: %s -> %s", payload.username, user.id)

    return AuthResponse(user_id=user.id, username=payload.username, session_key=raw_key)


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: AuthLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_login:{ip}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    account = (
        await db.execute(select(Account).where(Account.username == payload.username))
    ).scalar_one_or_none()

    if not account:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")

    now = datetime.utcnow()
    if account.locked_until and account.locked_until > now:
        remaining = int((account.locked_until - now).total_seconds() / 60) + 1
        raise HTTPException(status.HTTP_423_LOCKED, f"account locked, try again in {remaining} min")

    if not await verify_password(payload.password, account.password_hash):
        account.failed_attempts = (account.failed_attempts or 0) + 1
        if account.failed_attempts >= MAX_FAILED_ATTEMPTS:
            account.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            logger.warning("Account locked: %s after %d failures", payload.username, account.failed_attempts)
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")

    account.failed_attempts = 0
    account.locked_until = None
    account.last_login_at = now

    user = (
        await db.execute(select(User).where(User.id == account.linked_user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "linked user not found")

    raw_key, session_key = _create_session_key(user.id)
    db.add(session_key)

    await db.commit()
    logger.info("User logged in: %s", payload.username)

    return AuthResponse(user_id=user.id, username=payload.username, session_key=raw_key)
