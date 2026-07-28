import base64
import ipaddress
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .billing import get_available_balances_cents
from .config import settings
from .db import get_db
from .models import (
    EnterpriseAccessKey,
    EnterpriseAccountGrant,
    EnterpriseClient,
    RequestLog,
    User,
)
from .rate_limiter import rate_limiter
from .security import generate_id, hash_key, require_admin
from .usage_buffer import usage_buffer


ENTERPRISE_KEY_PREFIX = "cc_ent_"
MAX_ENTERPRISE_GRANTS = 200
MAX_IP_ALLOWLIST_ENTRIES = 50
ENTERPRISE_CODE_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,63}$"

public_router = APIRouter(prefix="/v1/enterprise", tags=["enterprise-reporting"])
admin_router = APIRouter(
    prefix="/admin",
    tags=["admin-enterprise-reporting"],
    dependencies=[Depends(require_admin)],
)


class EnterpriseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=ENTERPRISE_CODE_PATTERN)
    low_balance_threshold_cents: int = Field(default=0, ge=0)

    @field_validator("name", "code")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class EnterpriseUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    status: Optional[Literal["active", "disabled"]] = None
    low_balance_threshold_cents: Optional[int] = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class EnterpriseGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=32)
    account_code: str = Field(pattern=ENTERPRISE_CODE_PATTERN)
    status: Literal["active", "disabled"] = "active"

    @field_validator("user_id", "account_code")
    @classmethod
    def strip_grant_strings(cls, value: str) -> str:
        return value.strip()


class EnterpriseGrantReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[EnterpriseGrantInput]


def _normalize_ip_allowlist(values: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = raw.strip()
        if not value:
            raise ValueError("IP allowlist entries must not be blank")
        try:
            if "/" in value:
                item = str(ipaddress.ip_network(value, strict=False))
            else:
                item = str(ipaddress.ip_address(value))
        except ValueError as exc:
            raise ValueError(f"invalid IP address or CIDR: {value}") from exc
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


class EnterpriseKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    expires_at: Optional[datetime] = None
    ip_allowlist: Optional[list[str]] = Field(default=None, max_length=MAX_IP_ALLOWLIST_ENTRIES)

    @field_validator("name")
    @classmethod
    def strip_key_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("ip_allowlist")
    @classmethod
    def normalize_allowlist(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return _normalize_ip_allowlist(value)


class EnterpriseKeyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    status: Optional[Literal["active", "revoked"]] = None
    expires_at: Optional[datetime] = None
    ip_allowlist: Optional[list[str]] = Field(default=None, max_length=MAX_IP_ALLOWLIST_ENTRIES)

    @field_validator("name")
    @classmethod
    def strip_optional_key_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("ip_allowlist")
    @classmethod
    def normalize_optional_allowlist(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return _normalize_ip_allowlist(value)


@dataclass(frozen=True)
class EnterpriseAuthContext:
    key_id: str
    enterprise: EnterpriseClient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def generate_enterprise_key() -> str:
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return f"{ENTERPRISE_KEY_PREFIX}{token}"


def _key_fingerprint(key_hash: str) -> str:
    return f"sha256:{key_hash[:12]}"


def _key_is_effectively_active(key: EnterpriseAccessKey, now: Optional[datetime] = None) -> bool:
    current = now or _utcnow()
    return key.status == "active" and not (
        key.expires_at and _as_utc(key.expires_at) <= current
    )


def _key_display_status(key: EnterpriseAccessKey, now: Optional[datetime] = None) -> str:
    if key.status == "revoked":
        return "revoked"
    return "active" if _key_is_effectively_active(key, now) else "expired"


def _trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    for raw in settings.trusted_proxy_cidrs.split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return networks


def resolve_client_ip(request: Request) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    direct_raw = request.client.host if request.client else ""
    try:
        direct_ip = ipaddress.ip_address(direct_raw)
    except ValueError:
        return None

    trusted_networks = _trusted_proxy_networks()
    if any(direct_ip in network for network in trusted_networks):
        forwarded_values = [
            value.strip()
            for value in request.headers.get("x-forwarded-for", "").split(",")
            if value.strip()
        ]
        for forwarded in reversed(forwarded_values):
            try:
                forwarded_ip = ipaddress.ip_address(forwarded)
            except ValueError:
                continue
            if not any(forwarded_ip in network for network in trusted_networks):
                return forwarded_ip
        return None
    return direct_ip


def _ip_is_allowed(client_ip, encoded_allowlist: Optional[str]) -> bool:
    if not encoded_allowlist:
        return True
    if client_ip is None:
        return False
    try:
        entries = json.loads(encoded_allowlist)
    except (TypeError, ValueError):
        return False
    if not isinstance(entries, list):
        return False
    if not entries:
        return True
    for value in entries:
        try:
            if "/" in value and client_ip in ipaddress.ip_network(value, strict=False):
                return True
            if "/" not in value and client_ip == ipaddress.ip_address(value):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _auth_error(code: int) -> HTTPException:
    return HTTPException(status_code=code, detail="enterprise credential rejected")


async def authenticate_enterprise(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> EnterpriseAuthContext:
    authorization = request.headers.get("authorization", "")
    match = re.fullmatch(r"Bearer ([^\s]+)", authorization, flags=re.IGNORECASE)
    secret = match.group(1) if match else ""
    if not secret.startswith(ENTERPRISE_KEY_PREFIX):
        raise _auth_error(status.HTTP_401_UNAUTHORIZED)

    row = (
        await db.execute(
            select(EnterpriseAccessKey, EnterpriseClient)
            .join(EnterpriseClient, EnterpriseClient.id == EnterpriseAccessKey.enterprise_id)
            .where(EnterpriseAccessKey.key_hash == hash_key(secret))
            .limit(1)
        )
    ).first()
    if not row:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED)
    key, enterprise = row
    now = _utcnow()
    if key.status != "active" or (key.expires_at and _as_utc(key.expires_at) <= now):
        raise _auth_error(status.HTTP_401_UNAUTHORIZED)
    if enterprise.status != "active":
        raise _auth_error(status.HTTP_403_FORBIDDEN)
    if not _ip_is_allowed(resolve_client_ip(request), key.ip_allowlist):
        raise _auth_error(status.HTTP_403_FORBIDDEN)
    if not await rate_limiter.allow(
        f"enterprise-reporting:{key.id}", int(settings.enterprise_reporting_rpm)
    ):
        raise _auth_error(status.HTTP_429_TOO_MANY_REQUESTS)

    key.last_used_at = now
    await db.commit()
    return EnterpriseAuthContext(key_id=key.id, enterprise=enterprise)


def _enterprise_fields(enterprise: EnterpriseClient) -> dict:
    return {
        "id": enterprise.id,
        "name": enterprise.name,
        "code": enterprise.code,
        "status": enterprise.status,
        "low_balance_threshold_cents": int(enterprise.low_balance_threshold_cents or 0),
        "created_at": _iso(enterprise.created_at),
        "updated_at": _iso(enterprise.updated_at),
    }


def _grant_fields(grant: EnterpriseAccountGrant, user: Optional[User] = None) -> dict:
    result = {
        "id": grant.id,
        "user_id": grant.user_id,
        "account_code": grant.account_code,
        "status": grant.status,
        "created_at": _iso(grant.created_at),
        "updated_at": _iso(grant.updated_at),
    }
    if user is not None:
        result["user"] = {
            "username": user.username,
            "email": user.email,
            "external_id": user.external_id,
            "status": user.status,
        }
    return result


def _key_fields(key: EnterpriseAccessKey) -> dict:
    try:
        allowlist = json.loads(key.ip_allowlist) if key.ip_allowlist else []
    except (TypeError, ValueError):
        allowlist = []
    return {
        "id": key.id,
        "name": key.name,
        "fingerprint": _key_fingerprint(key.key_hash),
        "status": _key_display_status(key),
        "ip_allowlist": allowlist,
        "expires_at": _iso(key.expires_at),
        "last_used_at": _iso(key.last_used_at),
        "created_at": _iso(key.created_at),
    }


async def _get_enterprise_or_404(db: AsyncSession, enterprise_id: str) -> EnterpriseClient:
    enterprise = (
        await db.execute(select(EnterpriseClient).where(EnterpriseClient.id == enterprise_id))
    ).scalar_one_or_none()
    if enterprise is None:
        raise HTTPException(status_code=404, detail="enterprise not found")
    return enterprise


async def _enterprise_detail(db: AsyncSession, enterprise: EnterpriseClient) -> dict:
    grant_rows = (
        await db.execute(
            select(EnterpriseAccountGrant, User)
            .join(User, User.id == EnterpriseAccountGrant.user_id)
            .where(EnterpriseAccountGrant.enterprise_id == enterprise.id)
            .order_by(EnterpriseAccountGrant.account_code.asc())
        )
    ).all()
    keys = (
        await db.execute(
            select(EnterpriseAccessKey)
            .where(EnterpriseAccessKey.enterprise_id == enterprise.id)
            .order_by(EnterpriseAccessKey.created_at.desc())
        )
    ).scalars().all()
    result = _enterprise_fields(enterprise)
    result["accounts"] = [_grant_fields(grant, user) for grant, user in grant_rows]
    result["keys"] = [_key_fields(key) for key in keys]
    return result


@admin_router.get("/enterprise-clients")
async def list_enterprises(
    q: str = Query(default="", max_length=128),
    db: AsyncSession = Depends(get_db),
):
    statement = select(EnterpriseClient).order_by(EnterpriseClient.created_at.desc()).limit(200)
    search = q.strip()
    if search:
        pattern = f"%{search}%"
        statement = statement.where(
            or_(EnterpriseClient.name.like(pattern), EnterpriseClient.code.like(pattern))
        )
    enterprises = (await db.execute(statement)).scalars().all()
    enterprise_ids = [item.id for item in enterprises]
    grants = []
    keys = []
    if enterprise_ids:
        grants = (
            await db.execute(
                select(EnterpriseAccountGrant).where(
                    EnterpriseAccountGrant.enterprise_id.in_(enterprise_ids)
                )
            )
        ).scalars().all()
        keys = (
            await db.execute(
                select(EnterpriseAccessKey).where(EnterpriseAccessKey.enterprise_id.in_(enterprise_ids))
            )
        ).scalars().all()

    data = []
    for enterprise in enterprises:
        item = _enterprise_fields(enterprise)
        item["account_count"] = sum(
            1 for grant in grants if grant.enterprise_id == enterprise.id and grant.status == "active"
        )
        enterprise_keys = [key for key in keys if key.enterprise_id == enterprise.id]
        item["active_key_count"] = sum(1 for key in enterprise_keys if _key_is_effectively_active(key))
        last_used_values = [key.last_used_at for key in enterprise_keys if key.last_used_at]
        item["last_used_at"] = _iso(max(last_used_values)) if last_used_values else None
        data.append(item)
    return {"total": len(data), "data": data}


@admin_router.post("/enterprise-clients", status_code=status.HTTP_201_CREATED)
async def create_enterprise(payload: EnterpriseCreateRequest, db: AsyncSession = Depends(get_db)):
    existing = (
        await db.execute(select(EnterpriseClient.id).where(EnterpriseClient.code == payload.code))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="enterprise code already exists")
    enterprise = EnterpriseClient(
        id=generate_id("ent_"),
        name=payload.name,
        code=payload.code,
        status="active",
        low_balance_threshold_cents=payload.low_balance_threshold_cents,
    )
    db.add(enterprise)
    await db.commit()
    await db.refresh(enterprise)
    return await _enterprise_detail(db, enterprise)


@admin_router.get("/enterprise-clients/{enterprise_id}")
async def get_enterprise(enterprise_id: str, db: AsyncSession = Depends(get_db)):
    return await _enterprise_detail(db, await _get_enterprise_or_404(db, enterprise_id))


@admin_router.patch("/enterprise-clients/{enterprise_id}")
async def update_enterprise(
    enterprise_id: str,
    payload: EnterpriseUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    enterprise = await _get_enterprise_or_404(db, enterprise_id)
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        if value is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")
        setattr(enterprise, field, value)
    await db.commit()
    await db.refresh(enterprise)
    return await _enterprise_detail(db, enterprise)


@admin_router.put("/enterprise-clients/{enterprise_id}/accounts")
async def replace_enterprise_accounts(
    enterprise_id: str,
    payload: EnterpriseGrantReplaceRequest,
    db: AsyncSession = Depends(get_db),
):
    enterprise = await _get_enterprise_or_404(db, enterprise_id)
    active_count = sum(1 for account in payload.accounts if account.status == "active")
    if active_count > MAX_ENTERPRISE_GRANTS:
        raise HTTPException(status_code=422, detail="enterprise may have at most 200 active accounts")
    user_ids = [account.user_id for account in payload.accounts]
    account_codes = [account.account_code for account in payload.accounts]
    if len(user_ids) != len(set(user_ids)):
        raise HTTPException(status_code=422, detail="duplicate user_id")
    if len(account_codes) != len(set(account_codes)):
        raise HTTPException(status_code=422, detail="duplicate account_code")
    found_user_ids: set[str] = set()
    if user_ids:
        found_user_ids = set(
            (await db.execute(select(User.id).where(User.id.in_(user_ids)))).scalars().all()
        )
    missing_user_ids = sorted(set(user_ids) - found_user_ids)
    if missing_user_ids:
        raise HTTPException(status_code=422, detail={"missing_user_ids": missing_user_ids})

    await db.execute(
        delete(EnterpriseAccountGrant).where(
            EnterpriseAccountGrant.enterprise_id == enterprise_id
        )
    )
    db.add_all(
        [
            EnterpriseAccountGrant(
                id=generate_id("eag_"),
                enterprise_id=enterprise_id,
                user_id=account.user_id,
                account_code=account.account_code,
                status=account.status,
            )
            for account in payload.accounts
        ]
    )
    await db.commit()
    return await _enterprise_detail(db, enterprise)


@admin_router.post(
    "/enterprise-clients/{enterprise_id}/keys",
    status_code=status.HTTP_201_CREATED,
)
async def create_enterprise_access_key(
    enterprise_id: str,
    payload: EnterpriseKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    await _get_enterprise_or_404(db, enterprise_id)
    now = _utcnow()
    if payload.expires_at and _as_utc(payload.expires_at) <= now:
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    raw_key = generate_enterprise_key()
    key = EnterpriseAccessKey(
        id=generate_id("ek_"),
        enterprise_id=enterprise_id,
        name=payload.name,
        key_hash=hash_key(raw_key),
        status="active",
        expires_at=payload.expires_at,
        ip_allowlist=json.dumps(payload.ip_allowlist or [], separators=(",", ":")),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    response = _key_fields(key)
    response["api_key"] = raw_key
    return response


@admin_router.patch("/enterprise-keys/{key_id}")
async def update_enterprise_access_key(
    key_id: str,
    payload: EnterpriseKeyUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    key = (
        await db.execute(select(EnterpriseAccessKey).where(EnterpriseAccessKey.id == key_id))
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="enterprise key not found")
    if key.status == "revoked" and payload.status == "active":
        raise HTTPException(status_code=409, detail="revoked keys cannot be reactivated")
    if "expires_at" in payload.model_fields_set and payload.expires_at:
        if _as_utc(payload.expires_at) <= _utcnow():
            raise HTTPException(status_code=422, detail="expires_at must be in the future")
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        if field in {"name", "status"} and value is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")
        if field == "ip_allowlist":
            value = json.dumps(value or [], separators=(",", ":"))
        setattr(key, field, value)
    await db.commit()
    await db.refresh(key)
    return _key_fields(key)


async def _active_grants(db: AsyncSession, enterprise_id: str) -> list[EnterpriseAccountGrant]:
    return list(
        (
            await db.execute(
                select(EnterpriseAccountGrant)
                .where(
                    EnterpriseAccountGrant.enterprise_id == enterprise_id,
                    EnterpriseAccountGrant.status == "active",
                )
                .order_by(EnterpriseAccountGrant.account_code.asc())
            )
        ).scalars().all()
    )


@public_router.get("/balances")
async def enterprise_balances(
    response: Response,
    auth: EnterpriseAuthContext = Depends(authenticate_enterprise),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    grants = await _active_grants(db, auth.enterprise.id)
    user_ids = [grant.user_id for grant in grants]
    users_by_id: dict[str, User] = {}
    activity_by_user: dict[str, datetime] = {}
    if user_ids:
        users = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        users_by_id = {user.id: user for user in users}
        if len(users_by_id) != len(set(user_ids)):
            raise HTTPException(status_code=500, detail="enterprise account lookup failed")
        activity_rows = (
            await db.execute(
                select(RequestLog.user_id, func.max(RequestLog.created_at).label("last_activity_at"))
                .where(RequestLog.user_id.in_(user_ids))
                .group_by(RequestLog.user_id)
            )
        ).all()
        activity_by_user = {row.user_id: row.last_activity_at for row in activity_rows}

    pending_costs = {
        user.id: await usage_buffer.get_pending_cost(user.id)
        for user in users_by_id.values()
    }
    snapshots = await get_available_balances_cents(
        db,
        list(users_by_id.values()),
        pending_cost_cents_by_user=pending_costs,
    )
    data = []
    threshold = int(auth.enterprise.low_balance_threshold_cents or 0)
    for grant in grants:
        user = users_by_id.get(grant.user_id)
        if user is None:
            continue
        snapshot = snapshots.get(user.id, {})
        available = int(snapshot.get("available_cents", 0) or 0)
        balance_status = "insufficient" if available <= 0 else "low" if threshold > 0 and available <= threshold else "ok"
        data.append(
            {
                "account_code": grant.account_code,
                "account_status": user.status,
                "available_balance_cents": available,
                "available_balance_usd": available / 100,
                "balance_status": balance_status,
                "last_activity_at": _iso(activity_by_user.get(user.id)),
            }
        )
    now = _utcnow()
    return {
        "object": "enterprise.balance.list",
        "enterprise": {"code": auth.enterprise.code, "name": auth.enterprise.name},
        "currency": "usd_cents",
        "as_of": _iso(now),
        "total_available_balance_cents": sum(item["available_balance_cents"] for item in data),
        "data": data,
    }


@public_router.get("/usage-summary")
async def enterprise_usage_summary(
    response: Response,
    days: int = Query(default=7, ge=1, le=90),
    auth: EnterpriseAuthContext = Depends(authenticate_enterprise),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    end_at = _utcnow()
    start_at = end_at - timedelta(days=days)
    grants = await _active_grants(db, auth.enterprise.id)
    user_ids = [grant.user_id for grant in grants]
    aggregate_by_user = {}
    if user_ids:
        rows = (
            await db.execute(
                select(
                    RequestLog.user_id,
                    func.count(RequestLog.id).label("requests"),
                    func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(RequestLog.output_tokens), 0).label("output_tokens"),
                    func.coalesce(func.sum(RequestLog.image_count), 0).label("images"),
                    func.coalesce(func.sum(RequestLog.video_count), 0).label("videos"),
                    func.coalesce(func.sum(RequestLog.cost_cents), 0).label("cost_cents"),
                    func.max(RequestLog.created_at).label("last_activity_at"),
                )
                .where(
                    RequestLog.user_id.in_(user_ids),
                    RequestLog.created_at >= start_at,
                    RequestLog.created_at <= end_at,
                )
                .group_by(RequestLog.user_id)
            )
        ).all()
        aggregate_by_user = {row.user_id: row for row in rows}

    data = []
    for grant in grants:
        row = aggregate_by_user.get(grant.user_id)
        cost_cents = int(row.cost_cents or 0) if row else 0
        data.append(
            {
                "account_code": grant.account_code,
                "requests": int(row.requests or 0) if row else 0,
                "input_tokens": int(row.input_tokens or 0) if row else 0,
                "output_tokens": int(row.output_tokens or 0) if row else 0,
                "images": int(row.images or 0) if row else 0,
                "videos": int(row.videos or 0) if row else 0,
                "cost_cents": cost_cents,
                "cost_usd": cost_cents / 100,
                "last_activity_at": _iso(row.last_activity_at) if row else None,
            }
        )
    total_fields = ("requests", "input_tokens", "output_tokens", "images", "videos", "cost_cents")
    total = {field: sum(item[field] for item in data) for field in total_fields}
    total["cost_usd"] = total["cost_cents"] / 100
    return {
        "object": "enterprise.usage_summary",
        "enterprise": {"code": auth.enterprise.code, "name": auth.enterprise.name},
        "period": {"days": days, "start_at": _iso(start_at), "end_at": _iso(end_at)},
        "total": total,
        "data": data,
    }
