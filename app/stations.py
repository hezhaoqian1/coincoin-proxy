from __future__ import annotations

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import ApiKey, RequestLog, Station, StationAlias, StationApplication, StationBranding, StationCommissionLedgerEntry, StationCustomerLink, StationPayoutBatch, StationPricebookEntry, User
from .proxy import authenticate_user
from .router import IMAGE_ENDPOINTS, TEXT_ENDPOINTS, registry as model_registry
from .schemas import AdminStationCreateRequest, StationAliasCreateRequest, StationAliasUpdateRequest, StationApplicationCreateRequest, StationApplicationReviewRequest, StationBrandingUpdateRequest, StationCustomerCreateRequest, StationPayoutBatchCreateRequest, StationPayoutBatchMarkPaidRequest, StationPricebookUpdateRequest, StationSettlementUpdateRequest
from .security import encrypt_api_key, generate_api_key, generate_id, generate_referral_code, hash_key, require_admin


router = APIRouter(prefix="/v1/stations", tags=["stations"])


async def get_station_public_models_by_id(station_id: str, db: AsyncSession) -> list[dict]:
    station_id = (station_id or "").strip()
    if not station_id:
        return []
    return await _list_active_alias_models(station_id, db)


def _slugify_station_name(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return base[:48] or "station"


def _normalize_station_slug(value: str) -> str:
    slug = _slugify_station_name(value)
    if len(slug) > 64:
        slug = slug[:64].strip("-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}[a-z0-9]", slug) and len(slug) > 1:
        slug = re.sub(r"[^a-z0-9-]", "-", slug).strip("-")[:64]
    if not slug:
        slug = "station"
    return slug


async def _get_current_user(request: Request, db: AsyncSession) -> User:
    user = await authenticate_user(request, db)
    result = await db.execute(select(User).where(User.id == user.id))
    db_user = result.scalar_one_or_none()
    if not db_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return db_user


async def _get_owned_station(user_id: str, db: AsyncSession) -> Station:
    station = (
        await db.execute(
            select(Station)
            .where(Station.owner_user_id == user_id, Station.status == "active")
            .limit(1)
        )
    ).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="active station not found")
    return station


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _normalize_base_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/")


def _normalize_domain(value: str | None) -> str:
    domain = (value or "").strip().strip("/")
    domain = re.sub(r"^https?://", "", domain)
    return domain.split("/", 1)[0].strip("/")


def _join_path(base_url: str, *parts: str) -> str:
    path_parts = [str(part).strip("/") for part in parts if str(part or "").strip("/")]
    return "/".join([base_url, *path_parts]) if path_parts else base_url


def _build_station_urls(slug: str) -> dict:
    public_base_url = _normalize_base_url(settings.station_public_base_url or settings.self_base_url)
    portal_domain = _normalize_domain(settings.station_portal_domain)
    api_domain = _normalize_domain(settings.station_api_domain)
    api_base_url = _normalize_base_url(settings.station_api_base_url)
    portal_path_prefix = settings.station_portal_path_prefix or "/s"
    api_path_prefix = settings.station_api_path_prefix or "/v1"

    if portal_domain:
        portal_url = f"https://{slug}.{portal_domain}"
        portal_url_mode = "wildcard"
    elif public_base_url:
        portal_url = _join_path(public_base_url, portal_path_prefix, slug)
        portal_url_mode = "path"
    else:
        portal_url = ""
        portal_url_mode = "unconfigured"

    if api_domain:
        api_base_url = _join_path(f"https://{slug}.{api_domain}", api_path_prefix)
        api_url_mode = "wildcard"
    elif api_base_url:
        api_url_mode = "shared"
    elif public_base_url:
        api_base_url = _join_path(public_base_url, api_path_prefix)
        api_url_mode = "shared"
    else:
        api_base_url = ""
        api_url_mode = "unconfigured"

    return {
        "portal_url": portal_url,
        "api_base_url": api_base_url,
        "portal_url_mode": portal_url_mode,
        "api_url_mode": api_url_mode,
    }


def _station_public_payload(station: Station) -> dict:
    url_payload = _build_station_urls(station.slug)
    return {
        "id": station.id,
        "slug": station.slug,
        "display_name": station.display_name,
        "status": station.status,
        "mode": getattr(station, "mode", "commission_station") or "commission_station",
        "balance_cents": int(getattr(station, "balance_cents", 0) or 0),
        "currency": getattr(station, "currency", "usd_cents") or "usd_cents",
        "wholesale_tier": getattr(station, "wholesale_tier", "standard") or "standard",
        "default_text_alias": getattr(station, "default_text_alias", "") or "",
        "default_image_alias": getattr(station, "default_image_alias", "") or "",
        "commission_rate": station.commission_rate,
        "settlement_method": station.settlement_method,
        "settlement_payee_name": station.settlement_payee_name,
        "settlement_payee_account": station.settlement_payee_account,
        "settlement_qr_url": station.settlement_qr_url,
        "created_at": _iso(station.created_at),
        **url_payload,
    }


def _admin_station_payload(station: Station, owner: User | None = None, *, customer_count: int | None = None) -> dict:
    owner_payload = None
    if owner is not None:
        owner_payload = {
            "id": owner.id,
            "username": owner.username,
            "email": getattr(owner, "email", None),
            "status": owner.status,
        }
    return {
        **_station_public_payload(station),
        "owner_user_id": station.owner_user_id,
        "owner": owner_payload,
        "customer_count": customer_count,
        "request_limit_per_minute": getattr(station, "request_limit_per_minute", None),
        "daily_spend_limit_cents": getattr(station, "daily_spend_limit_cents", None),
        "monthly_spend_limit_cents": getattr(station, "monthly_spend_limit_cents", None),
        "suspended_reason": getattr(station, "suspended_reason", "") or "",
        "updated_at": _iso(getattr(station, "updated_at", None)),
    }


def _capability_group(capability: str) -> str:
    cap = (capability or "").strip()
    if cap in IMAGE_ENDPOINTS:
        return "image"
    if cap in TEXT_ENDPOINTS:
        return "text"
    if cap == "embeddings":
        return "embedding"
    return "text"


def _target_supports_capability(public_model, capability: str) -> bool:
    target_caps = set(getattr(public_model, "capabilities", ()) or ())
    if capability in target_caps:
        return True
    if capability in TEXT_ENDPOINTS and target_caps.intersection(TEXT_ENDPOINTS):
        return True
    return False


def _validate_alias_target(target_public_model_id: str, capability: str):
    model_registry.ensure_initialized()
    target = model_registry.get_public_model((target_public_model_id or "").strip())
    if not target:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target model not available")
    if not _target_supports_capability(target, capability):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target capability mismatch")
    return target


def _price_or_default(raw_value, default_value):
    value = raw_value if raw_value not in (None, 0, 0.0) else default_value
    return value or 0


def _validate_retail_price(public_model, *, input_price: int = 0, output_price: int = 0, image_price: float = 0.0) -> None:
    if int(input_price or 0) < int(getattr(public_model, "price_input_per_million", 0) or 0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="retail price below platform cost")
    if int(output_price or 0) < int(getattr(public_model, "price_output_per_million", 0) or 0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="retail price below platform cost")
    if float(image_price or 0.0) < float(getattr(public_model, "price_per_image_cents", 0.0) or 0.0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="retail price below platform cost")


def _alias_payload(alias: StationAlias) -> dict:
    return {
        "id": alias.id,
        "station_id": alias.station_id,
        "alias": alias.alias,
        "target_public_model_id": alias.target_public_model_id,
        "fallback_target_public_model_id": alias.fallback_target_public_model_id,
        "capability": alias.capability,
        "status": alias.status,
        "is_default_text": bool(alias.is_default_text),
        "is_default_image": bool(alias.is_default_image),
        "created_at": _iso(alias.created_at),
        "updated_at": _iso(alias.updated_at),
    }


def _pricebook_payload(entry: StationPricebookEntry) -> dict:
    return {
        "id": entry.id,
        "station_id": entry.station_id,
        "station_alias_id": entry.station_alias_id,
        "billable_sku": entry.billable_sku,
        "usage_unit_type": entry.usage_unit_type,
        "retail_input_per_million_cents": entry.retail_input_per_million_cents,
        "retail_output_per_million_cents": entry.retail_output_per_million_cents,
        "retail_price_per_image_cents": entry.retail_price_per_image_cents,
        "min_allowed_cents": entry.min_allowed_cents,
        "max_allowed_cents": entry.max_allowed_cents,
        "price_version": entry.price_version,
        "status": entry.status,
        "created_at": _iso(entry.created_at),
        "updated_at": _iso(entry.updated_at),
    }


def _branding_payload(branding: StationBranding | None, station: Station | None = None) -> dict:
    station_display = getattr(station, "display_name", "") if station is not None else ""
    return {
        "display_name": (getattr(branding, "display_name", "") or station_display or "").strip(),
        "logo_url": getattr(branding, "logo_url", "") if branding is not None else "",
        "favicon_url": getattr(branding, "favicon_url", "") if branding is not None else "",
        "support_email": getattr(branding, "support_email", "") if branding is not None else "",
        "support_link": getattr(branding, "support_link", "") if branding is not None else "",
        "docs_intro": getattr(branding, "docs_intro", "") if branding is not None else "",
        "terms_url": getattr(branding, "terms_url", "") if branding is not None else "",
        "updated_at": _iso(getattr(branding, "updated_at", None)) if branding is not None else None,
    }


def _serialize_station_model_alias(alias: StationAlias, price: StationPricebookEntry | None = None) -> dict:
    target = model_registry.get_public_model(alias.target_public_model_id)
    capabilities = list(getattr(target, "capabilities", ()) or [alias.capability])
    cached_input_price = 0
    if price:
        try:
            cached_input_price = round(
                float(price.retail_input_per_million_cents or 0) * float(settings.cache_discount_rate or 0),
                4,
            )
        except Exception:
            cached_input_price = 0
    return {
        "id": alias.alias,
        "object": "model",
        "created": int(alias.created_at.timestamp()) if alias.created_at else 1700000000,
        "owned_by": "station",
        "coincoin_station_id": alias.station_id,
        "coincoin_station_alias": alias.alias,
        "coincoin_resolved_public_model": alias.target_public_model_id,
        "coincoin_capabilities": capabilities,
        "coincoin_billable_sku": price.billable_sku if price else (getattr(target, "billable_sku", "") or alias.alias),
        "coincoin_routing_mode": "station_alias",
        "coincoin_default_for": [
            label
            for label, enabled in (
                ("text", bool(alias.is_default_text)),
                ("image", bool(alias.is_default_image)),
            )
            if enabled
        ],
        "coincoin_price_input_per_million": price.retail_input_per_million_cents if price else 0,
        "coincoin_price_cached_input_per_million": cached_input_price,
        "coincoin_price_output_per_million": price.retail_output_per_million_cents if price else 0,
        "coincoin_price_per_image_cents": price.retail_price_per_image_cents if price else 0,
    }


async def _list_active_alias_models(station_id: str, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(StationAlias, StationPricebookEntry)
            .outerjoin(StationPricebookEntry, StationPricebookEntry.station_alias_id == StationAlias.id)
            .where(StationAlias.station_id == station_id, StationAlias.status == "active")
            .order_by(StationAlias.alias.asc())
        )
    ).all()
    return [_serialize_station_model_alias(alias, price) for alias, price in rows]


async def list_station_public_models_for_user(user: User, db: AsyncSession) -> list[dict] | None:
    station_id = ""
    context = getattr(user, "_station_context", None)
    if isinstance(context, dict):
        if (context.get("status") or "active") != "active":
            return []
        station_id = str(context.get("station_id") or "").strip()
    if not station_id:
        station = (
            await db.execute(
                select(Station)
                .where(Station.owner_user_id == user.id, Station.status == "active")
                .limit(1)
            )
        ).scalar_one_or_none()
        if station:
            station_id = station.id
    if not station_id:
        return None

    return await _list_active_alias_models(station_id, db)


@router.get("/me/context")
async def get_my_station_context(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_current_user(request, db)
    link_row = (
        await db.execute(
            select(StationCustomerLink, Station)
            .join(Station, StationCustomerLink.station_id == Station.id)
            .where(StationCustomerLink.user_id == user.id)
            .limit(1)
        )
    ).first()
    if not link_row:
        return {"station": None, "branding": None, "aliases": []}

    link, station = link_row
    branding = (
        await db.execute(
            select(StationBranding).where(StationBranding.station_id == station.id).limit(1)
        )
    ).scalar_one_or_none()
    aliases = await _list_active_alias_models(station.id, db) if link.status == "active" and station.status == "active" else []
    return {
        "station": _station_public_payload(station),
        "branding": _branding_payload(branding, station),
        "customer_link": {
            "id": link.id,
            "status": link.status,
            "created_at": _iso(link.created_at),
        },
        "aliases": aliases,
    }


@router.get("/public/{slug}")
async def get_public_station(slug: str, db: AsyncSession = Depends(get_db)):
    station = (
        await db.execute(
            select(Station)
            .where(Station.slug == slug, Station.status == "active")
            .limit(1)
        )
    ).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="station not found")

    branding = (
        await db.execute(
            select(StationBranding).where(StationBranding.station_id == station.id).limit(1)
        )
    ).scalar_one_or_none()
    aliases = await _list_active_alias_models(station.id, db)
    return {
        "station": _station_public_payload(station),
        "branding": _branding_payload(branding, station),
        "aliases": aliases,
    }


@router.get("/application")
async def get_station_application(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_current_user(request, db)

    app_row = (
        await db.execute(
            select(StationApplication)
            .where(StationApplication.user_id == user.id)
            .order_by(StationApplication.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    station = (
        await db.execute(
            select(Station).where(Station.owner_user_id == user.id).limit(1)
        )
    ).scalar_one_or_none()

    return {
        "has_station": bool(station),
        "station": None if not station else _station_public_payload(station),
        "application": None if not app_row else {
            "id": app_row.id,
            "status": app_row.status,
            "station_name": app_row.station_name,
            "contact_handle": app_row.contact_handle,
            "traffic_source": app_row.traffic_source,
            "audience_note": app_row.audience_note,
            "settlement_method": app_row.settlement_method,
            "settlement_payee_name": app_row.settlement_payee_name,
            "settlement_payee_account": app_row.settlement_payee_account,
            "settlement_qr_url": app_row.settlement_qr_url,
            "review_note": app_row.review_note,
            "reviewed_by": app_row.reviewed_by,
            "reviewed_at": _iso(app_row.reviewed_at),
            "created_at": _iso(app_row.created_at),
        },
    }


@router.get("/me/summary")
async def get_station_summary(request: Request, db: AsyncSession = Depends(get_db)):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)

    customer_count = (
        await db.execute(
            select(func.count())
            .select_from(StationCustomerLink)
            .where(StationCustomerLink.station_id == station.id, StationCustomerLink.status == "active")
        )
    ).scalar_one()

    commission_rows = (
        await db.execute(
            select(
                StationCommissionLedgerEntry.status,
                func.count(StationCommissionLedgerEntry.id),
                func.coalesce(func.sum(StationCommissionLedgerEntry.commission_rmb_cents), 0),
            )
            .where(StationCommissionLedgerEntry.station_id == station.id)
            .group_by(StationCommissionLedgerEntry.status)
        )
    ).all()
    commission_summary = {
        "pending_rmb_cents": 0,
        "batched_rmb_cents": 0,
        "paid_rmb_cents": 0,
        "pending_count": 0,
        "batched_count": 0,
        "paid_count": 0,
    }
    for ledger_status, row_count, row_sum in commission_rows:
        key_prefix = ledger_status if ledger_status in {"pending", "batched", "paid"} else "pending"
        commission_summary[f"{key_prefix}_rmb_cents"] = int(row_sum or 0)
        commission_summary[f"{key_prefix}_count"] = int(row_count or 0)

    payout_rows = (
        await db.execute(
            select(
                StationPayoutBatch.status,
                func.count(StationPayoutBatch.id),
                func.coalesce(func.sum(StationPayoutBatch.total_commission_rmb_cents), 0),
                func.max(StationPayoutBatch.paid_at),
            )
            .where(StationPayoutBatch.station_id == station.id)
            .group_by(StationPayoutBatch.status)
        )
    ).all()
    payout_summary = {
        "pending_batch_count": 0,
        "pending_batch_total_rmb_cents": 0,
        "paid_batch_count": 0,
        "paid_batch_total_rmb_cents": 0,
        "last_paid_at": None,
    }
    for payout_status, row_count, row_sum, last_paid_at in payout_rows:
        if payout_status == "paid":
            payout_summary["paid_batch_count"] = int(row_count or 0)
            payout_summary["paid_batch_total_rmb_cents"] = int(row_sum or 0)
            payout_summary["last_paid_at"] = _iso(last_paid_at)
        else:
            payout_summary["pending_batch_count"] += int(row_count or 0)
            payout_summary["pending_batch_total_rmb_cents"] += int(row_sum or 0)

    return {
        "station": _station_public_payload(station),
        "customer_count": int(customer_count or 0),
        "commission_summary": {
            **commission_summary,
            "pending_rmb": commission_summary["pending_rmb_cents"] / 100,
            "batched_rmb": commission_summary["batched_rmb_cents"] / 100,
            "paid_rmb": commission_summary["paid_rmb_cents"] / 100,
        },
        "payout_summary": {
            **payout_summary,
            "pending_batch_total_rmb": payout_summary["pending_batch_total_rmb_cents"] / 100,
            "paid_batch_total_rmb": payout_summary["paid_batch_total_rmb_cents"] / 100,
        },
    }


@router.get("/me/commission-ledger")
async def list_my_station_commission_ledger(
    request: Request,
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = None,
):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)

    query = (
        select(StationCommissionLedgerEntry, User)
        .join(User, StationCommissionLedgerEntry.user_id == User.id)
        .where(StationCommissionLedgerEntry.station_id == station.id)
    )
    if status_filter:
        query = query.where(StationCommissionLedgerEntry.status == status_filter)
    rows = (
        await db.execute(query.order_by(StationCommissionLedgerEntry.created_at.desc()).limit(200))
    ).all()
    return {
        "station_id": station.id,
        "data": [
            {
                "id": entry.id,
                "user_id": entry.user_id,
                "username": user.username,
                "order_no": entry.order_no,
                "status": entry.status,
                "gross_rmb_cents": entry.gross_rmb_cents,
                "gross_rmb": entry.gross_rmb_cents / 100,
                "commission_rate": entry.commission_rate,
                "commission_rmb_cents": entry.commission_rmb_cents,
                "commission_rmb": entry.commission_rmb_cents / 100,
                "hold_until": _iso(entry.hold_until),
                "payout_batch_id": entry.payout_batch_id,
                "created_at": _iso(entry.created_at),
            }
            for entry, user in rows
        ],
    }


@router.get("/me/payout-batches")
async def list_my_station_payout_batches(request: Request, db: AsyncSession = Depends(get_db)):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    rows = (
        await db.execute(
            select(StationPayoutBatch)
            .where(StationPayoutBatch.station_id == station.id)
            .order_by(StationPayoutBatch.created_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return {
        "station_id": station.id,
        "data": [
            {
                "id": batch.id,
                "status": batch.status,
                "entry_count": batch.entry_count,
                "total_commission_rmb_cents": batch.total_commission_rmb_cents,
                "total_commission_rmb": batch.total_commission_rmb_cents / 100,
                "settlement_method": batch.settlement_method,
                "payee_name": batch.payee_name,
                "payee_account": batch.payee_account,
                "qr_url": batch.qr_url,
                "notes": batch.notes,
                "payment_reference": batch.payment_reference,
                "payment_screenshot_url": batch.payment_screenshot_url,
                "payment_note": batch.payment_note,
                "created_at": _iso(batch.created_at),
                "paid_at": _iso(batch.paid_at),
            }
            for batch in rows
        ],
    }


@router.post("/me/settlement")
async def update_station_settlement(
    payload: StationSettlementUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    station.settlement_method = (payload.settlement_method or "alipay_manual").strip() or "alipay_manual"
    station.settlement_payee_name = (payload.settlement_payee_name or "").strip()
    station.settlement_payee_account = (payload.settlement_payee_account or "").strip()
    station.settlement_qr_url = (payload.settlement_qr_url or "").strip()
    await db.commit()
    return {"success": True, "station": _station_public_payload(station)}


@router.get("/me/branding")
async def get_station_branding(request: Request, db: AsyncSession = Depends(get_db)):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    branding = (
        await db.execute(
            select(StationBranding).where(StationBranding.station_id == station.id).limit(1)
        )
    ).scalar_one_or_none()
    return {
        "station_id": station.id,
        "branding": _branding_payload(branding, station),
    }


@router.patch("/me/branding")
async def update_station_branding(
    payload: StationBrandingUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    branding = (
        await db.execute(
            select(StationBranding).where(StationBranding.station_id == station.id).limit(1)
        )
    ).scalar_one_or_none()
    if not branding:
        branding = StationBranding(station_id=station.id)
        db.add(branding)

    for field_name in (
        "display_name",
        "logo_url",
        "favicon_url",
        "support_email",
        "support_link",
        "docs_intro",
        "terms_url",
    ):
        next_value = getattr(payload, field_name)
        if next_value is not None:
            setattr(branding, field_name, next_value.strip())

    if payload.display_name is not None and payload.display_name.strip():
        station.display_name = payload.display_name.strip()

    await db.commit()
    return {
        "success": True,
        "station_id": station.id,
        "branding": _branding_payload(branding, station),
        "station": _station_public_payload(station),
    }


@router.get("/me/alias-targets")
async def list_station_alias_targets(request: Request, db: AsyncSession = Depends(get_db)):
    owner = await _get_current_user(request, db)
    await _get_owned_station(owner.id, db)
    return {
        "data": [
            {
                "id": model.public_id,
                "owned_by": model.owned_by,
                "capabilities": list(model.capabilities),
                "billable_sku": model.billable_sku,
                "price_input_per_million": model.price_input_per_million,
                "price_output_per_million": model.price_output_per_million,
                "price_per_image_cents": model.price_per_image_cents,
            }
            for model in model_registry.list_public_models()
        ]
    }


@router.get("/me/aliases")
async def list_station_aliases(request: Request, db: AsyncSession = Depends(get_db)):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    rows = (
        await db.execute(
            select(StationAlias, StationPricebookEntry)
            .outerjoin(StationPricebookEntry, StationPricebookEntry.station_alias_id == StationAlias.id)
            .where(StationAlias.station_id == station.id)
            .order_by(StationAlias.created_at.desc())
        )
    ).all()
    return {
        "station_id": station.id,
        "data": [
            {
                **_alias_payload(alias),
                "pricebook": None if price is None else _pricebook_payload(price),
            }
            for alias, price in rows
        ],
    }


@router.post("/me/aliases")
async def create_station_alias(
    payload: StationAliasCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    alias_name = payload.alias.strip()
    capability = (payload.capability or "chat/completions").strip() or "chat/completions"
    target = _validate_alias_target(payload.target_public_model_id, capability)
    existing = (
        await db.execute(
            select(StationAlias)
            .where(StationAlias.station_id == station.id, StationAlias.alias == alias_name)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="station alias already exists")

    retail_input = int(_price_or_default(payload.retail_input_per_million_cents, target.price_input_per_million))
    retail_output = int(_price_or_default(payload.retail_output_per_million_cents, target.price_output_per_million))
    retail_image = float(_price_or_default(payload.retail_price_per_image_cents, target.price_per_image_cents))
    _validate_retail_price(target, input_price=retail_input, output_price=retail_output, image_price=retail_image)

    alias = StationAlias(
        id=generate_id("sa_"),
        station_id=station.id,
        alias=alias_name,
        target_public_model_id=target.public_id,
        fallback_target_public_model_id=(payload.fallback_target_public_model_id or "").strip(),
        capability=capability,
        status="active",
        is_default_text=1 if payload.is_default_text else 0,
        is_default_image=1 if payload.is_default_image else 0,
        created_by_user_id=owner.id,
    )
    db.add(alias)
    await db.flush()

    pricebook = StationPricebookEntry(
        id=generate_id("sp_"),
        station_id=station.id,
        station_alias_id=alias.id,
        billable_sku=target.billable_sku or target.public_id,
        usage_unit_type="images" if _capability_group(capability) == "image" else "tokens",
        retail_input_per_million_cents=retail_input,
        retail_output_per_million_cents=retail_output,
        retail_price_per_image_cents=retail_image,
        min_allowed_cents=max(int(target.price_input_per_million or 0), int(target.price_output_per_million or 0)),
        max_allowed_cents=0,
        price_version=1,
        status="active",
    )
    db.add(pricebook)

    if payload.is_default_text:
        await db.execute(
            update(StationAlias)
            .where(StationAlias.station_id == station.id)
            .values(is_default_text=0)
        )
        station.default_text_alias = alias.alias
    if payload.is_default_image:
        await db.execute(
            update(StationAlias)
            .where(StationAlias.station_id == station.id)
            .values(is_default_image=0)
        )
        station.default_image_alias = alias.alias

    await db.commit()
    return {
        "success": True,
        "alias": _alias_payload(alias),
        "pricebook": _pricebook_payload(pricebook),
    }


@router.patch("/me/aliases/{alias_id}")
async def update_station_alias(
    alias_id: str,
    payload: StationAliasUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    alias = (
        await db.execute(
            select(StationAlias)
            .where(StationAlias.id == alias_id, StationAlias.station_id == station.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if not alias:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="station alias not found")

    if payload.target_public_model_id is not None:
        target = _validate_alias_target(payload.target_public_model_id, alias.capability)
        alias.target_public_model_id = target.public_id
    if payload.fallback_target_public_model_id is not None:
        alias.fallback_target_public_model_id = payload.fallback_target_public_model_id.strip()
    if payload.status is not None:
        alias.status = payload.status
    if payload.is_default_text is not None:
        alias.is_default_text = 1 if payload.is_default_text else 0
        if payload.is_default_text:
            await db.execute(
                update(StationAlias)
                .where(StationAlias.station_id == station.id, StationAlias.id != alias.id)
                .values(is_default_text=0)
            )
            station.default_text_alias = alias.alias
    if payload.is_default_image is not None:
        alias.is_default_image = 1 if payload.is_default_image else 0
        if payload.is_default_image:
            await db.execute(
                update(StationAlias)
                .where(StationAlias.station_id == station.id, StationAlias.id != alias.id)
                .values(is_default_image=0)
            )
            station.default_image_alias = alias.alias

    await db.commit()
    return {"success": True, "alias": _alias_payload(alias)}


@router.get("/me/pricebook")
async def list_station_pricebook(request: Request, db: AsyncSession = Depends(get_db)):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    rows = (
        await db.execute(
            select(StationPricebookEntry, StationAlias)
            .join(StationAlias, StationPricebookEntry.station_alias_id == StationAlias.id)
            .where(StationPricebookEntry.station_id == station.id)
            .order_by(StationAlias.alias.asc())
        )
    ).all()
    return {
        "station_id": station.id,
        "data": [
            {
                **_pricebook_payload(price),
                "alias": _alias_payload(alias),
            }
            for price, alias in rows
        ],
    }


@router.patch("/me/pricebook/{entry_id}")
async def update_station_pricebook_entry(
    entry_id: str,
    payload: StationPricebookUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    entry = (
        await db.execute(
            select(StationPricebookEntry)
            .where(StationPricebookEntry.id == entry_id, StationPricebookEntry.station_id == station.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pricebook entry not found")
    alias = (
        await db.execute(
            select(StationAlias)
            .where(StationAlias.id == entry.station_alias_id, StationAlias.station_id == station.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if not alias:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="station alias not found")
    target = _validate_alias_target(alias.target_public_model_id, getattr(alias, "capability", "chat/completions"))

    next_input = entry.retail_input_per_million_cents if payload.retail_input_per_million_cents is None else payload.retail_input_per_million_cents
    next_output = entry.retail_output_per_million_cents if payload.retail_output_per_million_cents is None else payload.retail_output_per_million_cents
    next_image = entry.retail_price_per_image_cents if payload.retail_price_per_image_cents is None else payload.retail_price_per_image_cents
    _validate_retail_price(target, input_price=next_input, output_price=next_output, image_price=next_image)

    entry.retail_input_per_million_cents = int(next_input or 0)
    entry.retail_output_per_million_cents = int(next_output or 0)
    entry.retail_price_per_image_cents = float(next_image or 0.0)
    entry.price_version = int(entry.price_version or 0) + 1
    if payload.status is not None:
        entry.status = payload.status

    await db.commit()
    return {"success": True, "pricebook": _pricebook_payload(entry)}


@router.post("/apply")
async def create_station_application(
    payload: StationApplicationCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_current_user(request, db)

    station = (
        await db.execute(select(Station).where(Station.owner_user_id == user.id).limit(1))
    ).scalar_one_or_none()
    if station:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="station already exists")

    existing_pending = (
        await db.execute(
            select(StationApplication)
            .where(
                StationApplication.user_id == user.id,
                StationApplication.status == "pending",
            )
            .order_by(StationApplication.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="pending application already exists")

    application = StationApplication(
        id=generate_id("sta_"),
        user_id=user.id,
        status="pending",
        station_name=payload.station_name.strip(),
        contact_handle=(payload.contact_handle or "").strip(),
        traffic_source=(payload.traffic_source or "").strip(),
        audience_note=payload.audience_note.strip(),
        settlement_method=(payload.settlement_method or "alipay_manual").strip() or "alipay_manual",
        settlement_payee_name=(payload.settlement_payee_name or "").strip(),
        settlement_payee_account=(payload.settlement_payee_account or "").strip(),
        settlement_qr_url=(payload.settlement_qr_url or "").strip(),
    )
    db.add(application)
    await db.commit()

    return {
        "success": True,
        "application_id": application.id,
        "status": application.status,
        "message": "station application submitted",
    }


@router.post("/me/customers")
async def create_station_customer(
    payload: StationCustomerCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)

    existing_link = (
        await db.execute(
            select(StationCustomerLink, User)
            .join(User, StationCustomerLink.user_id == User.id)
            .where(User.username == payload.username)
            .limit(1)
        )
    ).first()
    if existing_link:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username already belongs to a station user")

    existing_user = (
        await db.execute(select(User).where(User.username == payload.username).limit(1))
    ).scalar_one_or_none()
    if existing_user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username already exists")

    user = User(
        id=generate_id("u_"),
        username=payload.username,
        status="active",
        token_used=0,
        balance=0,
        referral_code=generate_referral_code(),
    )
    db.add(user)
    await db.flush()

    db.add(
        StationCustomerLink(
            id=generate_id("sclink_"),
            station_id=station.id,
            user_id=user.id,
            created_by_user_id=owner.id,
            status="active",
        )
    )

    raw_api_key = None
    if payload.create_api_key:
        raw_api_key = generate_api_key()
        db.add(
            ApiKey(
                id=generate_id("k_"),
                user_id=user.id,
                key_hash=hash_key(raw_api_key),
                encrypted_key=encrypt_api_key(raw_api_key),
                kind="api",
                status="active",
            )
        )

    await db.commit()
    return {
        "success": True,
        "station_id": station.id,
        "user_id": user.id,
        "username": user.username,
        "api_key": raw_api_key,
    }


@router.get("/me/customers")
async def list_station_customers(request: Request, db: AsyncSession = Depends(get_db)):
    owner = await _get_current_user(request, db)
    station = await _get_owned_station(owner.id, db)
    rows = (
        await db.execute(
            select(StationCustomerLink, User)
            .join(User, StationCustomerLink.user_id == User.id)
            .where(StationCustomerLink.station_id == station.id)
            .order_by(StationCustomerLink.created_at.desc())
            .limit(200)
        )
    ).all()
    return {
        "station_id": station.id,
        "data": [
            {
                "link_id": link.id,
                "user_id": user.id,
                "username": user.username,
                "status": link.status,
                "created_at": _iso(link.created_at),
            }
            for link, user in rows
        ],
    }


admin_router = APIRouter(prefix="/admin/stations", tags=["admin-stations"])


def _admin_guard(request: Request) -> None:
    require_admin(request)


@admin_router.get("", dependencies=[Depends(_admin_guard)])
async def list_admin_stations(
    db: AsyncSession = Depends(get_db),
    search: str | None = None,
    status_filter: str | None = None,
):
    customer_count = (
        select(func.count(StationCustomerLink.id))
        .where(StationCustomerLink.station_id == Station.id)
        .correlate(Station)
        .scalar_subquery()
    )
    query = (
        select(Station, User, customer_count)
        .join(User, Station.owner_user_id == User.id)
        .order_by(Station.created_at.desc())
    )
    if status_filter:
        query = query.where(Station.status == status_filter)
    if search:
        pat = f"%{search.strip()}%"
        query = query.where(
            Station.id.ilike(pat)
            | Station.slug.ilike(pat)
            | Station.display_name.ilike(pat)
            | User.id.ilike(pat)
            | User.username.ilike(pat)
            | User.email.ilike(pat)
        )
    rows = (await db.execute(query.limit(200))).all()
    return [_admin_station_payload(station, owner, customer_count=int(customer_count or 0)) for station, owner, customer_count in rows]


@admin_router.post("", dependencies=[Depends(_admin_guard)])
async def create_admin_station(payload: AdminStationCreateRequest, db: AsyncSession = Depends(get_db)):
    owner_filters = []
    if (payload.owner_user_id or "").strip():
        owner_filters.append(User.id == payload.owner_user_id.strip())
    if (payload.owner_username or "").strip():
        owner_filters.append(User.username == payload.owner_username.strip())
    if (payload.owner_email or "").strip():
        owner_filters.append(User.email == payload.owner_email.strip())
    if not owner_filters:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="owner user is required")
    owner = (await db.execute(select(User).where(*owner_filters).limit(1))).scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="owner user not found")

    existing_station = (
        await db.execute(select(Station).where(Station.owner_user_id == owner.id).limit(1))
    ).scalar_one_or_none()
    if existing_station:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="owner already has station")
    existing_link = (
        await db.execute(select(StationCustomerLink).where(StationCustomerLink.user_id == owner.id).limit(1))
    ).scalar_one_or_none()
    if existing_link:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="owner already belongs to a station")

    slug = _normalize_station_slug(payload.slug or payload.display_name)
    slug_exists = (await db.execute(select(Station.id).where(Station.slug == slug).limit(1))).scalar_one_or_none()
    if slug_exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="station slug already exists")

    mode = (payload.mode or "commission_station").strip() or "commission_station"
    if mode not in {"commission_station", "wholesale_station"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid station mode")

    station = Station(
        id=generate_id("st_"),
        owner_user_id=owner.id,
        application_id=None,
        slug=slug,
        display_name=payload.display_name.strip(),
        status="active",
        mode=mode,
        balance_cents=int(payload.balance_cents or 0),
        currency="usd_cents",
        wholesale_tier=(payload.wholesale_tier or "standard").strip() or "standard",
        commission_rate=float(payload.commission_rate),
        settlement_method=(payload.settlement_method or "alipay_manual").strip() or "alipay_manual",
        settlement_payee_name=(payload.settlement_payee_name or "").strip(),
        settlement_payee_account=(payload.settlement_payee_account or "").strip(),
        settlement_qr_url=(payload.settlement_qr_url or "").strip(),
    )
    db.add(station)
    db.add(
        StationCustomerLink(
            id=generate_id("sclink_"),
            station_id=station.id,
            user_id=owner.id,
            created_by_user_id=owner.id,
            status="active",
        )
    )
    db.add(StationBranding(station_id=station.id, display_name=station.display_name))

    created_alias = None
    created_pricebook = None
    if payload.create_default_alias:
        target_model_id = (payload.default_target_public_model_id or "").strip()
        if not target_model_id:
            target_model_id = model_registry.default_text_model_id or settings.fixed_model
        capability = (payload.default_capability or "chat/completions").strip() or "chat/completions"
        target = _validate_alias_target(target_model_id, capability)
        alias_name = payload.default_alias.strip()
        retail_input = int(_price_or_default(payload.retail_input_per_million_cents, target.price_input_per_million))
        retail_output = int(_price_or_default(payload.retail_output_per_million_cents, target.price_output_per_million))
        retail_image = float(_price_or_default(payload.retail_price_per_image_cents, target.price_per_image_cents))
        _validate_retail_price(target, input_price=retail_input, output_price=retail_output, image_price=retail_image)

        created_alias = StationAlias(
            id=generate_id("sa_"),
            station_id=station.id,
            alias=alias_name,
            target_public_model_id=target.public_id,
            fallback_target_public_model_id="",
            capability=capability,
            status="active",
            is_default_text=1 if _capability_group(capability) != "image" else 0,
            is_default_image=1 if _capability_group(capability) == "image" else 0,
            created_by_user_id=owner.id,
        )
        db.add(created_alias)
        await db.flush()
        created_pricebook = StationPricebookEntry(
            id=generate_id("sp_"),
            station_id=station.id,
            station_alias_id=created_alias.id,
            billable_sku=target.billable_sku or target.public_id,
            usage_unit_type="images" if _capability_group(capability) == "image" else "tokens",
            retail_input_per_million_cents=retail_input,
            retail_output_per_million_cents=retail_output,
            retail_price_per_image_cents=retail_image,
            min_allowed_cents=max(int(target.price_input_per_million or 0), int(target.price_output_per_million or 0)),
            max_allowed_cents=0,
            price_version=1,
            status="active",
        )
        db.add(created_pricebook)
        if created_alias.is_default_image:
            station.default_image_alias = created_alias.alias
        else:
            station.default_text_alias = created_alias.alias

    await db.commit()
    return {
        "success": True,
        "station": _admin_station_payload(station, owner, customer_count=1),
        "alias": None if created_alias is None else _alias_payload(created_alias),
        "pricebook": None if created_pricebook is None else _pricebook_payload(created_pricebook),
    }


@admin_router.get("/applications", dependencies=[Depends(_admin_guard)])
async def list_station_applications(db: AsyncSession = Depends(get_db), status_filter: str | None = None):
    query = select(StationApplication, User).join(User, StationApplication.user_id == User.id)
    if status_filter:
        query = query.where(StationApplication.status == status_filter)
    rows = (
        await db.execute(query.order_by(StationApplication.created_at.desc()).limit(200))
    ).all()

    return [
        {
            "id": app.id,
            "user_id": app.user_id,
            "username": user.username,
            "status": app.status,
            "station_name": app.station_name,
            "contact_handle": app.contact_handle,
            "traffic_source": app.traffic_source,
            "audience_note": app.audience_note,
            "settlement_method": app.settlement_method,
            "settlement_payee_name": app.settlement_payee_name,
            "settlement_payee_account": app.settlement_payee_account,
            "settlement_qr_url": app.settlement_qr_url,
            "review_note": app.review_note,
            "reviewed_by": app.reviewed_by,
            "reviewed_at": _iso(app.reviewed_at),
            "created_at": _iso(app.created_at),
        }
        for app, user in rows
    ]


@admin_router.post("/applications/{application_id}/review", dependencies=[Depends(_admin_guard)])
async def review_station_application(
    application_id: str,
    payload: StationApplicationReviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    reviewer = request.headers.get("x-admin-token") or request.headers.get("authorization") or "admin"
    app_row = (
        await db.execute(
            select(StationApplication).where(StationApplication.id == application_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="application not found")

    if app_row.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="application already reviewed")

    new_status = (payload.status or "").strip().lower()
    if new_status not in {"approved", "rejected"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid review status")

    app_row.status = new_status
    app_row.review_note = (payload.review_note or "").strip()
    app_row.reviewed_by = reviewer[:64]
    app_row.reviewed_at = datetime.utcnow()

    created_station = None
    if new_status == "approved":
        existing_station = (
            await db.execute(select(Station).where(Station.owner_user_id == app_row.user_id).limit(1))
        ).scalar_one_or_none()
        if not existing_station:
            base_slug = _slugify_station_name(app_row.station_name)
            slug = base_slug
            suffix = 1
            while (
                await db.execute(select(Station.id).where(Station.slug == slug).limit(1))
            ).scalar_one_or_none():
                suffix += 1
                slug = f"{base_slug}-{suffix}"

            created_station = Station(
                id=generate_id("st_"),
                owner_user_id=app_row.user_id,
                application_id=app_row.id,
                slug=slug,
                display_name=app_row.station_name,
                status="active",
                settlement_method=app_row.settlement_method,
                settlement_payee_name=app_row.settlement_payee_name,
                settlement_payee_account=app_row.settlement_payee_account,
                settlement_qr_url=app_row.settlement_qr_url,
            )
            db.add(created_station)
            db.add(
                StationCustomerLink(
                    id=generate_id("sclink_"),
                    station_id=created_station.id,
                    user_id=app_row.user_id,
                    created_by_user_id=app_row.user_id,
                    status="active",
                )
            )

    await db.commit()

    return {
        "success": True,
        "application_id": app_row.id,
        "status": app_row.status,
        "station": None if not created_station else {
            "id": created_station.id,
            "slug": created_station.slug,
            "display_name": created_station.display_name,
            "status": created_station.status,
        },
    }


@admin_router.get("/commission-ledger", dependencies=[Depends(_admin_guard)])
async def list_station_commission_ledger(
    db: AsyncSession = Depends(get_db),
    station_id: str | None = None,
    status_filter: str | None = None,
):
    query = (
        select(StationCommissionLedgerEntry, Station, User)
        .join(Station, StationCommissionLedgerEntry.station_id == Station.id)
        .join(User, StationCommissionLedgerEntry.user_id == User.id)
    )
    if station_id:
        query = query.where(StationCommissionLedgerEntry.station_id == station_id)
    if status_filter:
        query = query.where(StationCommissionLedgerEntry.status == status_filter)
    rows = (await db.execute(query.order_by(StationCommissionLedgerEntry.created_at.desc()).limit(200))).all()
    return [
        {
            "id": entry.id,
            "station_id": entry.station_id,
            "station_name": station.display_name,
            "user_id": entry.user_id,
            "username": user.username,
            "order_no": entry.order_no,
            "status": entry.status,
            "gross_rmb_cents": entry.gross_rmb_cents,
            "gross_rmb": entry.gross_rmb_cents / 100,
            "commission_rate": entry.commission_rate,
            "commission_rmb_cents": entry.commission_rmb_cents,
            "commission_rmb": entry.commission_rmb_cents / 100,
            "hold_until": _iso(entry.hold_until),
            "payout_batch_id": entry.payout_batch_id,
            "created_at": _iso(entry.created_at),
        }
        for entry, station, user in rows
    ]


@admin_router.post("/payout-batches", dependencies=[Depends(_admin_guard)])
async def create_station_payout_batch(
    payload: StationPayoutBatchCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    station = (
        await db.execute(select(Station).where(Station.id == payload.station_id).limit(1))
    ).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="station not found")

    now = datetime.utcnow()
    rows = (
        await db.execute(
            select(StationCommissionLedgerEntry)
            .where(
                StationCommissionLedgerEntry.station_id == payload.station_id,
                StationCommissionLedgerEntry.status == "pending",
                StationCommissionLedgerEntry.hold_until <= now,
            )
            .order_by(StationCommissionLedgerEntry.created_at.asc())
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no commission entries ready for payout")

    total_commission = sum(int(row.commission_rmb_cents or 0) for row in rows)
    if total_commission < int(settings.station_min_payout_rmb_cents):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"minimum payout not reached ({settings.station_min_payout_rmb_cents} cents)",
        )

    created_by = request.headers.get("x-admin-token") or request.headers.get("authorization") or "admin"
    batch = StationPayoutBatch(
        id=generate_id("spb_"),
        station_id=station.id,
        status="pending",
        entry_count=len(rows),
        total_commission_rmb_cents=total_commission,
        settlement_method=station.settlement_method,
        payee_name=station.settlement_payee_name,
        payee_account=station.settlement_payee_account,
        qr_url=station.settlement_qr_url,
        notes=(payload.notes or "").strip(),
        created_by=created_by[:64],
    )
    db.add(batch)
    await db.flush()

    for row in rows:
        row.status = "batched"
        row.payout_batch_id = batch.id

    await db.commit()
    return {
        "success": True,
        "batch_id": batch.id,
        "station_id": station.id,
        "entry_count": batch.entry_count,
        "total_commission_rmb_cents": batch.total_commission_rmb_cents,
        "total_commission_rmb": batch.total_commission_rmb_cents / 100,
    }


@admin_router.get("/payout-batches", dependencies=[Depends(_admin_guard)])
async def list_station_payout_batches(db: AsyncSession = Depends(get_db), station_id: str | None = None):
    query = select(StationPayoutBatch, Station).join(Station, StationPayoutBatch.station_id == Station.id)
    if station_id:
        query = query.where(StationPayoutBatch.station_id == station_id)
    rows = (await db.execute(query.order_by(StationPayoutBatch.created_at.desc()).limit(200))).all()
    return [
        {
            "id": batch.id,
            "station_id": batch.station_id,
            "station_name": station.display_name,
            "status": batch.status,
            "entry_count": batch.entry_count,
            "total_commission_rmb_cents": batch.total_commission_rmb_cents,
            "total_commission_rmb": batch.total_commission_rmb_cents / 100,
            "settlement_method": batch.settlement_method,
            "payee_name": batch.payee_name,
            "payee_account": batch.payee_account,
            "qr_url": batch.qr_url,
            "notes": batch.notes,
            "payment_reference": batch.payment_reference,
            "payment_screenshot_url": batch.payment_screenshot_url,
            "payment_note": batch.payment_note,
            "paid_by": batch.paid_by,
            "created_at": _iso(batch.created_at),
            "paid_at": _iso(batch.paid_at),
        }
        for batch, station in rows
    ]


@admin_router.post("/payout-batches/{batch_id}/mark-paid", dependencies=[Depends(_admin_guard)])
async def mark_station_payout_batch_paid(
    batch_id: str,
    payload: StationPayoutBatchMarkPaidRequest | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    batch = (
        await db.execute(
            select(StationPayoutBatch).where(StationPayoutBatch.id == batch_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    if batch.status == "paid":
        return {"success": True, "batch_id": batch.id, "status": batch.status}

    rows = (
        await db.execute(
            select(StationCommissionLedgerEntry).where(StationCommissionLedgerEntry.payout_batch_id == batch.id)
        )
    ).scalars().all()
    for row in rows:
        row.status = "paid"

    reviewer = request.headers.get("x-admin-token") or request.headers.get("authorization") or "admin"
    batch.status = "paid"
    batch.paid_by = reviewer[:64]
    batch.paid_at = datetime.utcnow()
    payload = payload or StationPayoutBatchMarkPaidRequest()
    batch.payment_reference = (payload.payment_reference or "").strip()
    batch.payment_screenshot_url = (payload.payment_screenshot_url or "").strip()
    batch.payment_note = (payload.payment_note or "").strip()
    await db.commit()
    return {
        "success": True,
        "batch_id": batch.id,
        "status": batch.status,
        "paid_at": _iso(batch.paid_at),
        "payment_reference": batch.payment_reference,
        "payment_screenshot_url": batch.payment_screenshot_url,
        "payment_note": batch.payment_note,
    }
