"""The referral program in the cabinet: the stats, the invitees, bonus days."""

from typing import Any

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter
from pydantic import BaseModel

from src.application.common.dao import ReferralDao, SettingsDao, SubscriptionDao
from src.core.enums import ReferralAccrualStrategy, SubscriptionStatus
from src.infrastructure.services.s.bonus import BonusDays, BonusDaysError

from .deps import CurrentActor
from .errors import CabinetError

router = APIRouter(prefix="/referrals/me")

_BONUS_ERRORS = {
    "none": "BONUS_DAYS_INSUFFICIENT",
    "unlimited": "BONUS_DAYS_PLAN_NOT_ALLOWED",
    "no-plan": "BONUS_DAYS_PLAN_NOT_ALLOWED",
}


@router.get("")
@inject
async def stats(
    actor: CurrentActor,
    settings_dao: FromDishka[SettingsDao],
    referral_dao: FromDishka[ReferralDao],
    bonus: FromDishka[BonusDays],
) -> dict[str, Any]:
    settings = await settings_dao.get()
    program = settings.referral
    user = actor.user
    counts = await referral_dao.get_user_referral_stats(user.id)
    referrer = await referral_dao.get_by_referred_id(user.id)
    # one-time invite links (s) replace the permanent ref_ link
    permanent = not settings.extra.s.invites.enabled
    return {
        "referral_code": user.referral_code if program.enable and permanent else None,
        "referrer_id": referrer.referrer.id if referrer else None,
        "points": f"{user.points}.00",
        "bonus_days": await bonus.kept(user.id) if await bonus.enabled() else 0,
        "counts_by_level": {
            "1": counts.referrals_level_1,
            "2": counts.referrals_level_2,
        },
        "program": {
            "enabled": program.enable,
            "reward_type": program.reward.type.value,
            "reward_strategy": program.reward.strategy.value,
            "reward_amounts": {
                str(int(level)): str(amount) for level, amount in program.reward.config.items()
            },
            "invitee_reward_type": program.reward.type.value,
            "invitee_reward_amount": 0,
            "accrual_strategy": (
                "FIRST_PAYMENT"
                if program.accrual_strategy == ReferralAccrualStrategy.ON_FIRST_PAYMENT
                else "EVERY_PAYMENT"
            ),
            "max_level": int(program.level),
        },
    }


@router.get("/redeem-targets")
@inject
async def redeem_targets(
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    bonus: FromDishka[BonusDays],
) -> dict[str, Any]:
    current = await subscription_dao.get_current(actor.user.id)
    usable = (
        await bonus.enabled()
        and current is not None
        and current.status != SubscriptionStatus.DELETED
        and not current.is_unlimited
    )
    return {"subscription_ids": [current.id] if usable and current else []}


class RedeemBody(BaseModel):
    subscription_id: int
    days: int


@router.post("/actions/redeem-days")
@inject
async def redeem_days(
    body: RedeemBody,
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    bonus: FromDishka[BonusDays],
) -> dict[str, Any]:
    if not await bonus.enabled():
        raise CabinetError(409, "BONUS_DAYS_INSUFFICIENT", "Bonus days are off")
    current = await subscription_dao.get_current(actor.user.id)
    if current is None or current.id != body.subscription_id:
        raise CabinetError(404, "SUBSCRIPTION_NOT_FOUND", "Subscription not found")
    kept = await bonus.kept(actor.user.id)
    # the bot adds all kept days at once
    if body.days != kept:
        raise CabinetError(409, "BONUS_DAYS_ABOVE_LIMIT", f"Only all {kept} days at once")
    try:
        taken = await bonus.take(actor.user)
    except BonusDaysError as error:
        code = _BONUS_ERRORS.get(str(error), "BONUS_DAYS_INSUFFICIENT")
        raise CabinetError(409, code, str(error)) from error
    return {
        "subscription_id": current.id,
        "expire_at": taken.expire_at.isoformat(),
        "bonus_days": 0,
        "pending": False,
    }


@router.get("/invited")
@inject
async def invited(
    actor: CurrentActor,
    referral_dao: FromDishka[ReferralDao],
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    page, page_size = max(page, 1), min(max(page_size, 1), 100)
    total = await referral_dao.get_referrals_count(actor.user.id)
    items = await referral_dao.get_referrals_list(
        actor.user.id, limit=page_size, offset=(page - 1) * page_size
    )
    return {
        "items": [
            {
                "name": item.referred.name if item.referred else None,
                "level": int(item.level),
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "reward_amounts": {},
            }
            for item in items
        ],
        "total": total,
    }
