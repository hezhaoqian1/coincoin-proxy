"""
Web authentication endpoints — register + login with username/password.
Session keys (kind='session') are issued for Dashboard access only;
they cannot be used on billing endpoints (chat/completions, responses).
"""
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .emailer import send_verification_email
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import Account, ApiKey, EmailVerificationCode, User
from .rate_limiter import rate_limiter
from .schemas import (
    AuthLoginRequest,
    AuthProfileResponse,
    AuthRegisterCheckCodeRequest,
    AuthRegisterCheckCodeResponse,
    AuthRegisterRequest,
    AuthRegisterSendCodeRequest,
    AuthRegisterSendCodeResponse,
    AuthRegisterResponse,
    AuthResendEmailRequest,
    AuthResponse,
    AuthSendEmailCodeRequest,
    AuthVerifyCurrentEmailRequest,
    AuthVerifyEmailRequest,
)
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
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def _allowed_email_domains() -> set[str]:
    return {
        item.strip().lower().lstrip("@")
        for item in (settings.allowed_email_domains or "").split(",")
        if item.strip()
    }


def _normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "请输入有效邮箱")

    domain = email.rsplit("@", 1)[-1]
    allowed = _allowed_email_domains()
    if allowed and domain not in allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请使用 Gmail、Outlook、iCloud、QQ、163 等主流邮箱")
    return email


def _hash_secret(value: str, scope: str) -> str:
    payload = f"{settings.key_pepper}:{scope}:{value}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_email_code(code: str) -> str:
    return _hash_secret(code.strip(), "email-code")


def _hash_ip(ip: str) -> str:
    return _hash_secret(ip, "ip")


def _register_verification_id(email: str, ip: str) -> str:
    digest = _hash_secret(f"{email}|{ip}", "register-email-verification")
    return f"regv_{digest[:24]}"


def _new_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _latest_open_email_code(db: AsyncSession, user_id: str) -> EmailVerificationCode | None:
    result = await db.execute(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.user_id == user_id,
            EmailVerificationCode.purpose == "register",
            EmailVerificationCode.consumed_at.is_(None),
        )
        .order_by(EmailVerificationCode.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_open_email_code_by_email(
    db: AsyncSession,
    email: str,
    *,
    purpose: str = "register",
) -> EmailVerificationCode | None:
    result = await db.execute(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.email == email,
            EmailVerificationCode.purpose == purpose,
            EmailVerificationCode.consumed_at.is_(None),
        )
        .order_by(EmailVerificationCode.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _queue_email_code(db: AsyncSession, *, user_id: str, email: str, ip: str) -> str:
    code = _new_email_code()
    ttl_minutes = max(1, int(settings.email_verification_ttl_minutes or 10))
    verification = EmailVerificationCode(
        id=generate_id("ev_"),
        user_id=user_id,
        email=email,
        code_hash=_hash_email_code(code),
        purpose="register",
        attempts=0,
        expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes),
        consumed_at=None,
        ip_hash=_hash_ip(ip),
        created_at=datetime.utcnow(),
    )
    db.add(verification)
    return code


async def _send_or_fail(email: str, code: str) -> None:
    ttl_minutes = max(1, int(settings.email_verification_ttl_minutes or 10))
    result = await send_verification_email(email=email, code=code, ttl_minutes=ttl_minutes)
    if not result.sent:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="验证码邮件发送失败，请稍后再试",
        )


async def _assert_register_email_available(
    db: AsyncSession,
    *,
    email: str,
    username: str | None = None,
) -> None:
    email_owner = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if not email_owner:
        return

    if username and email_owner.username == username:
        account = (
            await db.execute(select(Account).where(Account.linked_user_id == email_owner.id))
        ).scalar_one_or_none()
        if account and account.username == username and getattr(account, "status", "active") in {"pending_email", "email_send_failed"}:
            return

    raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")


async def _user_from_session(request: Request, db: AsyncSession) -> tuple[User, ApiKey]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing session")
    raw_key = auth.split(" ", 1)[1].strip()
    if not raw_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing session")

    key_hash = hash_key(raw_key)
    api_key = (
        await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.status == "active"))
    ).scalar_one_or_none()
    if not api_key or getattr(api_key, "kind", "api") != "session":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session")
    if api_key.expires_at and _utc_naive(api_key.expires_at) and _utc_naive(api_key.expires_at) < datetime.utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired, please login again")

    user = (
        await db.execute(select(User).where(User.id == api_key.user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session")
    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user blocked")
    return user, api_key


def _profile_response(user: User) -> AuthProfileResponse:
    email = getattr(user, "email", None)
    verified_at = getattr(user, "email_verified_at", None)
    return AuthProfileResponse(
        user_id=user.id,
        username=getattr(user, "username", None),
        email=email,
        email_verified_at=verified_at,
        email_verification_required=bool(email and not verified_at),
        )


async def _send_email_code_background(email: str, code: str) -> None:
    ttl_minutes = max(1, int(settings.email_verification_ttl_minutes or 10))
    result = await send_verification_email(email=email, code=code, ttl_minutes=ttl_minutes)
    if not result.sent:
        logger.warning("background verification email send failed for %s: %s", email, result.error)


@router.post("/register/send-code", response_model=AuthRegisterSendCodeResponse)
async def register_send_code(
    payload: AuthRegisterSendCodeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_register_send_code:{ip}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    email = _normalize_email(payload.email)
    await _assert_register_email_available(db, email=email)

    latest = await _latest_open_email_code_by_email(db, email)
    if latest and latest.created_at:
        cooldown = max(0, int(settings.email_resend_cooldown_seconds or 60))
        if _utc_naive(latest.created_at) and _utc_naive(latest.created_at) > datetime.utcnow() - timedelta(seconds=cooldown):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "发送太频繁，请稍后再试")

    verification_id = _register_verification_id(email, ip)
    code = await _queue_email_code(db, user_id=verification_id, email=email, ip=ip)
    await db.commit()
    background_tasks.add_task(_send_email_code_background, email, code)
    return AuthRegisterSendCodeResponse(verification_id=verification_id, email=email)


@router.post("/register/check-code", response_model=AuthRegisterCheckCodeResponse)
async def register_check_code(
    payload: AuthRegisterCheckCodeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_register_check_code:{ip}:{payload.verification_id}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    result = await db.execute(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.user_id == payload.verification_id,
            EmailVerificationCode.purpose == "register",
        )
        .order_by(EmailVerificationCode.created_at.desc())
        .limit(1)
    )
    verification = result.scalar_one_or_none()
    if not verification:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "验证码会话不存在，请重新发送")

    now = datetime.utcnow()
    if verification.consumed_at:
        return AuthRegisterCheckCodeResponse(
            verification_id=payload.verification_id,
            email=verification.email,
        )
    if _utc_naive(verification.expires_at) and _utc_naive(verification.expires_at) < now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码已过期，请重新发送")
    if int(verification.attempts or 0) >= max(1, int(settings.email_max_attempts or 5)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "验证码错误次数过多，请重新发送")

    submitted_hash = _hash_email_code(payload.code.strip())
    if not hmac.compare_digest(submitted_hash, verification.code_hash):
        verification.attempts = int(verification.attempts or 0) + 1
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码不正确")

    verification.consumed_at = now
    await db.commit()
    return AuthRegisterCheckCodeResponse(
        verification_id=payload.verification_id,
        email=verification.email,
    )


@router.post("/register", response_model=AuthRegisterResponse)
async def register(
    payload: AuthRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_register:{ip}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    email = _normalize_email(payload.email)
    username = payload.username.strip()
    verification_id = (payload.verification_id or "").strip()
    verification_code = (payload.verification_code or "").strip()
    if not verification_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先发送验证码")
    if not verification_code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请输入验证码")

    verification = (
        await db.execute(
            select(EmailVerificationCode)
            .where(
                EmailVerificationCode.user_id == verification_id,
                EmailVerificationCode.email == email,
                EmailVerificationCode.purpose == "register",
            )
            .order_by(EmailVerificationCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not verification:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码已失效，请重新发送")

    now = datetime.utcnow()
    if _utc_naive(verification.expires_at) and _utc_naive(verification.expires_at) < now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码已过期，请重新发送")
    if int(verification.attempts or 0) >= max(1, int(settings.email_max_attempts or 5)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "验证码错误次数过多，请重新发送")

    submitted_hash = _hash_email_code(verification_code)
    if not hmac.compare_digest(submitted_hash, verification.code_hash):
        verification.attempts = int(verification.attempts or 0) + 1
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码不正确")

    verification.consumed_at = now

    existing_account = (
        await db.execute(select(Account).where(Account.username == username))
    ).scalar_one_or_none()
    if existing_account and getattr(existing_account, "status", "active") not in {"pending_email", "email_send_failed"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")

    await _assert_register_email_available(db, email=email, username=username)

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

    if existing_account:
        user = (
            await db.execute(select(User).where(User.id == existing_account.linked_user_id))
        ).scalar_one_or_none()
        if not user or user.email != email:
            raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")
    else:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

    if user and user.email and user.email != email:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")

    if not user:
        user = User(
            id=generate_id("u_"),
            username=username,
            email=email,
            email_verified_at=None,
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
    else:
        user.email = email
        user.email_verified_at = None
        if not user.referred_by:
            user.referred_by = referrer_id
        if not user.register_ip:
            user.register_ip = ip

    if existing_account:
        account = existing_account
        account.password_hash = await hash_password(payload.password)
        account.status = "pending_email"
    else:
        account = Account(
            id=generate_id("acc_"),
            username=username,
            password_hash=await hash_password(payload.password),
            linked_user_id=user.id,
            status="pending_email",
        )
        db.add(account)

    user.email_verified_at = now
    account.status = "active"
    account.failed_attempts = 0
    account.locked_until = None
    account.last_login_at = now

    raw_key, session_key = _create_session_key(user.id)
    db.add(session_key)
    await db.commit()

    logger.info("User registered with verified email: %s -> %s", username, user.id)

    return AuthRegisterResponse(
        user_id=user.id,
        username=username,
        email=email,
        status="active",
        session_key=raw_key,
    )


@router.post("/verify-email", response_model=AuthResponse)
async def verify_email(
    payload: AuthVerifyEmailRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_verify:{ip}:{payload.user_id}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    user = (
        await db.execute(select(User).where(User.id == payload.user_id))
    ).scalar_one_or_none()
    if not user or not user.email:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "verification session not found")

    account = (
        await db.execute(select(Account).where(Account.linked_user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")

    verification = await _latest_open_email_code(db, user.id)
    if not verification:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码已失效，请重新发送")

    now = datetime.utcnow()
    if _utc_naive(verification.expires_at) and _utc_naive(verification.expires_at) < now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码已过期，请重新发送")

    if int(verification.attempts or 0) >= max(1, int(settings.email_max_attempts or 5)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "验证码错误次数过多，请重新发送")

    submitted_hash = _hash_email_code(payload.code.strip())
    if not hmac.compare_digest(submitted_hash, verification.code_hash):
        verification.attempts = int(verification.attempts or 0) + 1
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码不正确")

    verification.consumed_at = now
    user.email_verified_at = now
    account.status = "active"
    account.failed_attempts = 0
    account.locked_until = None
    account.last_login_at = now

    raw_key, session_key = _create_session_key(user.id)
    db.add(session_key)
    await db.commit()

    logger.info("User email verified: %s -> %s", account.username, user.id)
    return AuthResponse(user_id=user.id, username=account.username, session_key=raw_key)


@router.post("/resend-verification")
async def resend_verification(
    payload: AuthResendEmailRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_resend:{ip}:{payload.user_id}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    user = (
        await db.execute(select(User).where(User.id == payload.user_id))
    ).scalar_one_or_none()
    if not user or not user.email:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "verification session not found")

    if user.email_verified_at:
        return {"status": "already_verified", "user_id": user.id, "email": user.email}

    latest = await _latest_open_email_code(db, user.id)
    if latest and latest.created_at:
        cooldown = max(0, int(settings.email_resend_cooldown_seconds or 60))
        if _utc_naive(latest.created_at) and _utc_naive(latest.created_at) > datetime.utcnow() - timedelta(seconds=cooldown):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "发送太频繁，请稍后再试")

    code = await _queue_email_code(db, user_id=user.id, email=user.email, ip=ip)
    await db.commit()
    background_tasks.add_task(_send_email_code_background, user.email, code)
    return {"status": "email_verification_required", "user_id": user.id, "email": user.email}


@router.get("/me", response_model=AuthProfileResponse)
async def get_auth_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user, _ = await _user_from_session(request, db)
    return _profile_response(user)


@router.post("/me/email/send-code", response_model=AuthProfileResponse)
async def send_current_user_email_code(
    payload: AuthSendEmailCodeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    user, _ = await _user_from_session(request, db)
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_email_send:{ip}:{user.id}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")

    email = _normalize_email(payload.email)
    email_owner = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if email_owner and email_owner.id != user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    latest = await _latest_open_email_code(db, user.id)
    if latest and latest.created_at:
        cooldown = max(0, int(settings.email_resend_cooldown_seconds or 60))
        if _utc_naive(latest.created_at) and _utc_naive(latest.created_at) > datetime.utcnow() - timedelta(seconds=cooldown):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "发送太频繁，请稍后再试")

    user.email = email
    user.email_verified_at = None
    code = await _queue_email_code(db, user_id=user.id, email=email, ip=ip)
    await db.commit()
    background_tasks.add_task(_send_email_code_background, email, code)
    return _profile_response(user)


@router.post("/me/email/verify", response_model=AuthProfileResponse)
async def verify_current_user_email(
    payload: AuthVerifyCurrentEmailRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user, _ = await _user_from_session(request, db)
    ip = _client_ip(request)
    if not await rate_limiter.allow(f"auth_email_verify:{ip}:{user.id}", AUTH_RATE_LIMIT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try later")
    if not user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先填写邮箱")

    verification = await _latest_open_email_code(db, user.id)
    if not verification or verification.email != user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码已失效，请重新发送")

    now = datetime.utcnow()
    if _utc_naive(verification.expires_at) and _utc_naive(verification.expires_at) < now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码已过期，请重新发送")
    if int(verification.attempts or 0) >= max(1, int(settings.email_max_attempts or 5)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "验证码错误次数过多，请重新发送")

    submitted_hash = _hash_email_code(payload.code.strip())
    if not hmac.compare_digest(submitted_hash, verification.code_hash):
        verification.attempts = int(verification.attempts or 0) + 1
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码不正确")

    verification.consumed_at = now
    user.email_verified_at = now
    await db.commit()
    return _profile_response(user)


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

    user = (
        await db.execute(select(User).where(User.id == account.linked_user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "linked user not found")

    if getattr(account, "status", "active") in {"pending_email", "email_send_failed"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "请先验证邮箱")

    account.failed_attempts = 0
    account.locked_until = None
    account.last_login_at = now

    raw_key, session_key = _create_session_key(user.id)
    db.add(session_key)

    await db.commit()
    logger.info("User logged in: %s", payload.username)

    return AuthResponse(user_id=user.id, username=payload.username, session_key=raw_key)
