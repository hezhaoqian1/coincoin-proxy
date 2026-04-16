"""
Shared referral reward logic — used by both payment.py and webhook.py.

Rules:
- 5% commission (configurable)
- Max 3 rewarded orders per referred user
- Cumulative cap $50 per referred user
- New referred user gets bonus on first confirmed order
- Same-IP referrals are blocked at registration (auth.py)
"""
import logging

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .finance_summary import ensure_finance_summary_initialized, increment_finance_summary
from .models import ReferralReward, User
from .security import generate_id

logger = logging.getLogger("coincoin.referral")


async def process_referral_reward(
    user: User,
    add_cents: int,
    order_no: str,
    db: AsyncSession,
) -> int:
    """
    Process referral reward after a successful payment.
    Returns total reward_cents issued (0 if none).
    """
    if not user.referred_by or settings.referral_commission_rate <= 0:
        return 0

    reward_count = (await db.execute(
        select(func.count())
        .select_from(ReferralReward)
        .where(
            ReferralReward.referrer_id == user.referred_by,
            ReferralReward.referred_id == user.id,
        )
    )).scalar() or 0

    if reward_count >= settings.referral_max_rewards_per_user:
        logger.debug("referral: max rewards reached for user=%s", user.id)
        return 0

    existing_total = (await db.execute(
        select(func.coalesce(func.sum(ReferralReward.reward_cents), 0))
        .where(
            ReferralReward.referrer_id == user.referred_by,
            ReferralReward.referred_id == user.id,
        )
    )).scalar() or 0

    remaining_cap = settings.referral_reward_cap_cents - existing_total
    if remaining_cap <= 0:
        logger.debug("referral: cap reached for user=%s", user.id)
        return 0

    raw_reward = max(1, int(add_cents * settings.referral_commission_rate))
    reward_cents = min(raw_reward, remaining_cap)

    referrer = (
        await db.execute(select(User).where(User.id == user.referred_by).with_for_update())
    ).scalar_one_or_none()

    if not referrer or referrer.status != "active":
        return 0

    await ensure_finance_summary_initialized(db, referrer.id, commit=False)
    referrer.balance += reward_cents
    db.add(ReferralReward(
        id=generate_id("rr_"),
        referrer_id=referrer.id,
        referred_id=user.id,
        order_no=order_no,
        order_amount_cents=add_cents,
        reward_cents=reward_cents,
    ))
    await increment_finance_summary(db, referrer.id, bonus_cents=reward_cents)
    logger.info(
        "referral reward: referrer=%s +%dcents from user=%s order=%s (%d/%d)",
        referrer.id, reward_cents, user.id, order_no,
        reward_count + 1, settings.referral_max_rewards_per_user,
    )

    # First-purchase bonus for the referred user
    if reward_count == 0 and settings.referral_new_user_bonus_cents > 0:
        await ensure_finance_summary_initialized(db, user.id, commit=False)
        user.balance += settings.referral_new_user_bonus_cents
        await increment_finance_summary(db, user.id, bonus_cents=settings.referral_new_user_bonus_cents)
        logger.info(
            "referral bonus: user=%s +%dcents (first purchase)",
            user.id, settings.referral_new_user_bonus_cents,
        )

    return reward_cents
