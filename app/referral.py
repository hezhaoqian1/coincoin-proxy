"""
Shared referral reward logic.

Current product rules:
- invited user gets $10 after verified registration
- referrer gets $5 after the invited user registers
- referrer gets another $5 after the invited user first calls the API
- invited user gets another $20 after first purchase
- referrer gets 20% of credited API balance on referred purchases
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import ReferralReward, User
from .security import generate_id

logger = logging.getLogger("coincoin.referral")

REWARD_SIGNUP_INVITED = "signup_invited_bonus"
REWARD_SIGNUP_REFERRER = "signup_referrer_bonus"
REWARD_FIRST_USAGE_REFERRER = "first_usage_referrer_bonus"
REWARD_FIRST_PURCHASE_INVITED = "first_purchase_invited_bonus"
REWARD_PURCHASE_COMMISSION = "purchase_commission"


def _reward_type(row: ReferralReward) -> str:
    return getattr(row, "reward_type", None) or REWARD_PURCHASE_COMMISSION


def _recipient_id(row: ReferralReward) -> str:
    return getattr(row, "recipient_id", None) or row.referrer_id


def _bonus_key(referred_id: str, reward_type: str, order_no: str = "") -> str:
    if order_no:
        return f"referral:{reward_type}:{referred_id}:{order_no}"
    return f"referral:{reward_type}:{referred_id}"


async def _reward_exists(
    db: AsyncSession,
    *,
    referrer_id: str,
    referred_id: str,
    reward_type: str,
    order_no: str = "",
) -> bool:
    if order_no:
        query = select(ReferralReward).where(
            ReferralReward.referrer_id == referrer_id,
            ReferralReward.referred_id == referred_id,
            ReferralReward.order_no == order_no,
        )
        if reward_type == REWARD_PURCHASE_COMMISSION:
            query = query.where(
                or_(
                    ReferralReward.reward_type == reward_type,
                    ReferralReward.reward_type.is_(None),
                )
            )
        else:
            query = query.where(ReferralReward.reward_type == reward_type)
    else:
        query = select(ReferralReward).where(
            ReferralReward.referrer_id == referrer_id,
            ReferralReward.referred_id == referred_id,
        )
        if reward_type == REWARD_PURCHASE_COMMISSION:
            query = query.where(
                or_(
                    ReferralReward.reward_type == reward_type,
                    ReferralReward.reward_type.is_(None),
                )
            )
        else:
            query = query.where(ReferralReward.reward_type == reward_type)
    return (await db.execute(query.limit(1))).scalar_one_or_none() is not None


async def _credit_reward(
    *,
    db: AsyncSession,
    referrer_id: str,
    referred_id: str,
    recipient: User,
    reward_type: str,
    reward_cents: int,
    order_no: str = "",
    order_amount_cents: int = 0,
) -> int:
    reward_cents = int(reward_cents or 0)
    if reward_cents <= 0:
        return 0

    if await _reward_exists(
        db,
        referrer_id=referrer_id,
        referred_id=referred_id,
        reward_type=reward_type,
        order_no=order_no,
    ):
        return 0

    await ensure_finance_summary_initialized(db, recipient.id, commit=False)
    recipient.balance = int(recipient.balance or 0) + reward_cents
    db.add(
        ReferralReward(
            id=generate_id("rr_"),
            referrer_id=referrer_id,
            referred_id=referred_id,
            recipient_id=recipient.id,
            reward_type=reward_type,
            idempotency_key=_bonus_key(referred_id, reward_type, order_no),
            order_no=order_no,
            order_amount_cents=order_amount_cents,
            reward_cents=reward_cents,
            created_at=datetime.utcnow(),
        )
    )
    await increment_finance_summary(db, recipient.id, bonus_cents=reward_cents)
    logger.info(
        "referral reward: type=%s recipient=%s +%dcents referrer=%s referred=%s order=%s",
        reward_type,
        recipient.id,
        reward_cents,
        referrer_id,
        referred_id,
        order_no,
    )
    return reward_cents


async def process_signup_referral_rewards(user: User, db: AsyncSession) -> int:
    """Credit registration-time rewards for a verified referred user."""
    referred_by = getattr(user, "referred_by", None)
    if not referred_by:
        return 0

    referrer = (
        await db.execute(select(User).where(User.id == referred_by).with_for_update())
    ).scalar_one_or_none()
    if not referrer or referrer.status != "active":
        return 0

    total = 0
    total += await _credit_reward(
        db=db,
        referrer_id=referrer.id,
        referred_id=user.id,
        recipient=user,
        reward_type=REWARD_SIGNUP_INVITED,
        reward_cents=settings.referral_signup_bonus_cents,
    )
    total += await _credit_reward(
        db=db,
        referrer_id=referrer.id,
        referred_id=user.id,
        recipient=referrer,
        reward_type=REWARD_SIGNUP_REFERRER,
        reward_cents=settings.referral_signup_referrer_bonus_cents,
    )
    return total


async def process_first_usage_referral_reward(user_id: str, db: AsyncSession) -> int:
    """Credit the referrer after the invited user first produces billable usage."""
    user = (
        await db.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    referred_by = getattr(user, "referred_by", None)
    if not user or not referred_by:
        return 0

    referrer = (
        await db.execute(select(User).where(User.id == referred_by).with_for_update())
    ).scalar_one_or_none()
    if not referrer or referrer.status != "active":
        return 0

    return await _credit_reward(
        db=db,
        referrer_id=referrer.id,
        referred_id=user.id,
        recipient=referrer,
        reward_type=REWARD_FIRST_USAGE_REFERRER,
        reward_cents=settings.referral_first_usage_referrer_bonus_cents,
    )


async def process_referral_reward(
    user: User,
    add_cents: int,
    order_no: str,
    db: AsyncSession,
) -> int:
    """
    Process referral rewards after a successful payment.
    Returns total reward_cents issued across referrer and invited user.
    """
    referred_by = getattr(user, "referred_by", None)
    if not referred_by:
        return 0

    referrer = (
        await db.execute(select(User).where(User.id == referred_by).with_for_update())
    ).scalar_one_or_none()
    if not referrer or referrer.status != "active":
        return 0

    total = 0
    first_purchase_already_rewarded = await _reward_exists(
        db,
        referrer_id=referrer.id,
        referred_id=user.id,
        reward_type=REWARD_FIRST_PURCHASE_INVITED,
    ) or await _reward_exists(
        db,
        referrer_id=referrer.id,
        referred_id=user.id,
        reward_type=REWARD_PURCHASE_COMMISSION,
    )
    if not first_purchase_already_rewarded:
        total += await _credit_reward(
            db=db,
            referrer_id=referrer.id,
            referred_id=user.id,
            recipient=user,
            reward_type=REWARD_FIRST_PURCHASE_INVITED,
            reward_cents=settings.referral_new_user_bonus_cents,
            order_no=order_no,
            order_amount_cents=add_cents,
        )

    if settings.referral_commission_rate > 0:
        raw_reward = max(1, int(add_cents * settings.referral_commission_rate))

        existing_purchase_count = (
            await db.execute(
                select(func.count())
                .select_from(ReferralReward)
                .where(
                    ReferralReward.referrer_id == referrer.id,
                    ReferralReward.referred_id == user.id,
                    or_(
                        ReferralReward.reward_type == REWARD_PURCHASE_COMMISSION,
                        ReferralReward.reward_type.is_(None),
                    ),
                )
            )
        ).scalar() or 0
        max_rewards = int(settings.referral_max_rewards_per_user or 0)
        if max_rewards <= 0 or existing_purchase_count < max_rewards:
            existing_total = (
                await db.execute(
                    select(func.coalesce(func.sum(ReferralReward.reward_cents), 0)).where(
                        ReferralReward.referrer_id == referrer.id,
                        ReferralReward.referred_id == user.id,
                        or_(
                            ReferralReward.reward_type == REWARD_PURCHASE_COMMISSION,
                            ReferralReward.reward_type.is_(None),
                        ),
                    )
                )
            ).scalar() or 0
            cap = int(settings.referral_reward_cap_cents or 0)
            reward_cents = raw_reward
            if cap > 0:
                reward_cents = min(raw_reward, max(0, cap - int(existing_total or 0)))

            total += await _credit_reward(
                db=db,
                referrer_id=referrer.id,
                referred_id=user.id,
                recipient=referrer,
                reward_type=REWARD_PURCHASE_COMMISSION,
                reward_cents=reward_cents,
                order_no=order_no,
                order_amount_cents=add_cents,
            )

    return total


def build_referral_record(referred: User, rewards: list[ReferralReward]) -> dict:
    referrer_reward_cents = 0
    referred_reward_cents = 0
    has_usage = False
    has_purchase = False
    latest_at = getattr(referred, "created_at", None)

    for reward in rewards:
        recipient_id = _recipient_id(reward)
        reward_type = _reward_type(reward)
        reward_cents = int(getattr(reward, "reward_cents", 0) or 0)
        if recipient_id == getattr(referred, "referred_by", None):
            referrer_reward_cents += reward_cents
        elif recipient_id == referred.id:
            referred_reward_cents += reward_cents

        if reward_type == REWARD_FIRST_USAGE_REFERRER:
            has_usage = True
        if reward_type in {REWARD_FIRST_PURCHASE_INVITED, REWARD_PURCHASE_COMMISSION}:
            has_purchase = True
        if reward_type == REWARD_PURCHASE_COMMISSION and int(getattr(reward, "order_amount_cents", 0) or 0) > 0:
            has_purchase = True
        created_at = getattr(reward, "created_at", None)
        if created_at and (latest_at is None or created_at > latest_at):
            latest_at = created_at

    if has_purchase:
        status = "持续充值中"
        next_step = "持续充值奖励中"
    elif has_usage:
        status = "已开始使用"
        next_step = "等待首次充值"
    else:
        status = "已注册"
        next_step = "等待首次 API 调用"

    return {
        "user_id": referred.id,
        "username": getattr(referred, "username", None),
        "email": getattr(referred, "email", None),
        "status": status,
        "next_step": next_step,
        "referrer_reward_cents": referrer_reward_cents,
        "referrer_reward_usd": referrer_reward_cents / 100,
        "referred_reward_cents": referred_reward_cents,
        "referred_reward_usd": referred_reward_cents / 100,
        "created_at": referred.created_at.isoformat() + "Z" if getattr(referred, "created_at", None) else None,
        "last_progress_at": latest_at.isoformat() + "Z" if latest_at else None,
    }
