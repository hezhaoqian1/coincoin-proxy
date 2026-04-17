from __future__ import annotations

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import ApiKey, Station, StationApplication, StationCommissionLedgerEntry, StationCustomerLink, StationPayoutBatch, User
from .proxy import authenticate_user
from .schemas import StationApplicationCreateRequest, StationApplicationReviewRequest, StationCustomerCreateRequest, StationPayoutBatchCreateRequest, StationPayoutBatchMarkPaidRequest, StationSettlementUpdateRequest
from .security import generate_api_key, generate_id, generate_referral_code, hash_key, require_admin


router = APIRouter(prefix="/v1/stations", tags=["stations"])


def _slugify_station_name(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return base[:48] or "station"


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


def _station_public_payload(station: Station) -> dict:
    return {
        "id": station.id,
        "slug": station.slug,
        "display_name": station.display_name,
        "status": station.status,
        "commission_rate": station.commission_rate,
        "settlement_method": station.settlement_method,
        "settlement_payee_name": station.settlement_payee_name,
        "settlement_payee_account": station.settlement_payee_account,
        "settlement_qr_url": station.settlement_qr_url,
        "created_at": _iso(station.created_at),
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
