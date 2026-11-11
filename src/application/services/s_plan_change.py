"""Changing the plan with what is left of the current one counted.

Upstream charges the full price of the new plan, starts the term today and lets
the days left on the current plan go. Here the buyer picks one of two ways:

- keep the end date: the difference of the daily prices is paid for the days
  that are left;
- a new term from today: what is left of the current plan comes off the price.

Going to a cheaper plan costs nothing then; what is left over is not paid back.

A day of the current plan is worth what the buyer paid for it: the last paid
transaction for that plan, discounts included, in the currency of the payment
being made. A trial, a gift or a free purchase leaves nothing to count.

On a plan whose traffic never resets the traffic is what was bought, so what is
left is the smaller of the days and the traffic left: 95 GB of 100 used in five
days of thirty leave 5 %, not 25/30. Keeping the end date is not offered for a
new plan like that: the change starts its traffic afresh, and a few days of the
price difference must not buy a whole new allowance.

A plan with fewer devices than are connected asks which ones go; they are
removed before the lower limit reaches the panel, so no device is ever over it.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional, Union
from uuid import UUID

from loguru import logger
from remnapy.enums.users import TrafficLimitStrategy
from remnapy.models.hwid import HwidDeviceDto

from src.application.common import Remnawave
from src.application.common.dao import SubscriptionDao, TransactionDao
from src.application.dto import PlanDto, PriceDetailsDto, SubscriptionDto, TransactionDto
from src.application.dto.plan import PlanSnapshotDto
from src.application.dto.s_plan_change import ChangeMode, PlanChangeDto
from src.core.enums import Currency, SubscriptionStatus
from src.core.utils.converters import gb_to_bytes
from src.core.utils.time import datetime_now

DAY_SECONDS = Decimal(86400)
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Leftover:
    """The unused part of the current subscription."""

    expire_at: datetime
    days: Decimal
    daily: Decimal
    # The days the value left is worth: fewer than ``days`` when the traffic
    # bought runs out sooner.
    worth_days: Optional[Decimal] = None
    traffic_left_gb: Optional[Decimal] = None
    traffic_limit_gb: Optional[int] = None

    @property
    def value(self) -> Decimal:
        days = self.days if self.worth_days is None else min(self.days, self.worth_days)
        return self.daily * days


def traffic_is_bought(limit_gb: int, strategy: TrafficLimitStrategy) -> bool:
    """Traffic that never resets is the thing bought, not just a cap."""
    return limit_gb > 0 and strategy == TrafficLimitStrategy.NO_RESET


def keeps_expiry(plan: Union[PlanDto, PlanSnapshotDto]) -> bool:
    """Whether the new ``plan`` may run to the current end date."""
    return not traffic_is_bought(plan.traffic_limit, plan.traffic_limit_strategy)


def paid_daily(transaction: TransactionDto) -> Decimal:
    """What a day of the plan bought by ``transaction`` cost."""
    pricing = transaction.pricing
    amount = pricing.final_amount
    # A change paid only a part; the plan itself was worth its full price.
    if pricing.change and pricing.change.plan_amount is not None:
        amount = pricing.change.plan_amount
    return amount / transaction.plan_snapshot.duration


def leftover_of(
    subscription: Optional[SubscriptionDto],
    transactions: list[TransactionDto],
    currency: Currency,
    now: datetime,
    used_bytes: Optional[float] = None,
) -> Optional[Leftover]:
    """What is left of ``subscription``; ``transactions`` newest first.

    ``used_bytes`` is the traffic used, needed where the traffic was bought.
    """
    if subscription is None or subscription.is_trial or subscription.is_unlimited:
        return None

    seconds = (subscription.expire_at - now).total_seconds()
    if seconds <= 0:
        return None

    plan_id = subscription.plan_snapshot.id
    paid = next(
        (
            t
            for t in transactions
            if t.is_completed
            and not t.is_test
            and t.plan_snapshot.id == plan_id
            and t.plan_snapshot.duration > 0
        ),
        None,
    )
    if paid is None or paid.currency != currency:
        return None

    daily = paid_daily(paid)
    if daily <= 0:
        return None

    days = Decimal(str(seconds)) / DAY_SECONDS
    if not traffic_is_bought(subscription.traffic_limit, subscription.traffic_limit_strategy):
        return Leftover(subscription.expire_at, days, daily)

    if used_bytes is None:
        return None
    limit = Decimal(gb_to_bytes(subscription.traffic_limit))
    left = max(Decimal(0), limit - Decimal(int(used_bytes)))
    if left <= 0:
        return None
    # The traffic left is worth its share of the days bought with it.
    return Leftover(
        subscription.expire_at,
        days,
        daily,
        worth_days=Decimal(paid.plan_snapshot.duration) * left / limit,
        traffic_left_gb=left / gb_to_bytes(1),
        traffic_limit_gb=subscription.traffic_limit,
    )


def quote(
    price: PriceDetailsDto,
    days: int,
    leftover: Leftover,
    mode: ChangeMode,
    round_amount: Callable[[Decimal], Decimal],
) -> PriceDetailsDto:
    """``price`` of the new plan for ``days``, with ``leftover`` counted."""
    plan_amount = price.final_amount
    credit = Decimal(0)

    if mode == ChangeMode.KEEP_EXPIRY:
        due = plan_amount / days * leftover.days - leftover.value
    else:
        credit = min(leftover.value, plan_amount)
        due = plan_amount - credit

    return PriceDetailsDto(
        original_amount=price.original_amount,
        discount_percent=price.discount_percent,
        final_amount=round_amount(due) if due > 0 else Decimal(0),
        change=PlanChangeDto(mode=mode, plan_amount=plan_amount, credit=credit.quantize(CENT)),
    )


def kept_subscription(subscription: SubscriptionDto, plan: PlanSnapshotDto) -> SubscriptionDto:
    """The new plan on the current end date."""
    return SubscriptionDto(
        user_remna_id=subscription.user_remna_id,
        status=SubscriptionStatus.ACTIVE,
        is_trial=False,
        traffic_limit=plan.traffic_limit,
        device_limit=plan.device_limit,
        traffic_limit_strategy=plan.traffic_limit_strategy,
        tag=plan.tag,
        internal_squads=plan.internal_squads,
        external_squad=plan.external_squad,
        expire_at=subscription.expire_at,
        url=subscription.url,
        plan_snapshot=plan,
    )


def devices_to_drop(devices: list[HwidDeviceDto], device_limit: int) -> int:
    """How many of ``devices`` do not fit in ``device_limit`` (0 is no limit)."""
    if device_limit <= 0:
        return 0
    return max(0, len(devices) - device_limit)


async def drop_devices(remnawave: Remnawave, user_remna_id: UUID, hwids: list[str]) -> None:
    for hwid in hwids:
        try:
            await remnawave.delete_device(user_remna_id, hwid)
        except Exception as error:
            logger.warning(f"Device '{hwid}' was not removed for the plan change: {error}")


class PlanChange:
    def __init__(
        self,
        subscription_dao: SubscriptionDao,
        transaction_dao: TransactionDao,
        remnawave: Remnawave,
    ) -> None:
        self.subscription_dao = subscription_dao
        self.transaction_dao = transaction_dao
        self.remnawave = remnawave

    async def leftover(self, user_id: int, currency: Currency) -> Optional[Leftover]:
        subscription = await self.subscription_dao.get_current(user_id)
        if subscription is None:
            return None
        transactions = await self.transaction_dao.get_by_user(user_id)
        used_bytes = None
        if traffic_is_bought(subscription.traffic_limit, subscription.traffic_limit_strategy):
            remna_user = await self.remnawave.get_user_by_uuid(subscription.user_remna_id)
            used_bytes = remna_user.used_traffic_bytes if remna_user else None
        return leftover_of(subscription, transactions, currency, datetime_now(), used_bytes)

    async def devices(self, user_id: int) -> list[HwidDeviceDto]:
        subscription = await self.subscription_dao.get_current(user_id)
        if subscription is None:
            return []
        return await self.remnawave.get_devices(subscription.user_remna_id)
