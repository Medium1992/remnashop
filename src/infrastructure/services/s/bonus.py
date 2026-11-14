"""Referral days kept until the referrer takes them (docs/s/README.md).

Upstream adds a referral reward of days to the referrer's subscription at once
and loses it when there is none (or only a trial). With bonus days on, days are
kept instead: a reward of days stays "not issued" in ``referral_rewards`` and
the referrer takes all of them when it suits, from the invite window. They go
to the current subscription (a trial or a lapsed one included); without one, a
subscription is made on the plan the admin chose. Rewards lost before the
switch was turned on are still "not issued", so they are kept too.
"""

from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from src.application.common import EventPublisher
from src.application.common.dao import (
    PlanDao,
    ReferralDao,
    SettingsDao,
    SubscriptionDao,
    UserDao,
)
from src.application.common.uow import UnitOfWork
from src.application.dto import UserDto
from src.application.events import ReferralRewardReceivedEvent
from src.application.use_cases.referral.commands.rewards import (
    GiveReferrerReward,
    GiveReferrerRewardDto,
)
from src.application.use_cases.subscription.commands.management import (
    AddSubscriptionDuration,
    AddSubscriptionDurationDto,
)
from src.application.use_cases.subscription.commands.set_plan import (
    SetUserSubscription,
    SetUserSubscriptionDto,
)
from src.application.use_cases.user.commands.profile_edit import ChangeUserPoints
from src.core.enums import ReferralRewardType, SubscriptionStatus
from src.infrastructure.database.dao.s_bonus import BonusDaysDao


@dataclass(frozen=True, kw_only=True)
class BonusDaysKeptEvent(ReferralRewardReceivedEvent):
    """A reward of days kept for later; ``total`` is all that is kept now."""

    total: int = field(default=0)

    @property
    def event_key(self) -> str:
        return "event-s-bonus.kept"


class BonusDaysError(Exception):
    """Why the days could not be taken; the value is the reason's i18n suffix."""


@dataclass(frozen=True)
class BonusDaysTaken:
    days: int
    expire_at: datetime


class SGiveReferrerReward(GiveReferrerReward):
    """Upstream's reward, except that days are kept while bonus days are on."""

    def __init__(
        self,
        uow: UnitOfWork,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        referral_dao: ReferralDao,
        event_publisher: EventPublisher,
        change_user_points: ChangeUserPoints,
        add_subscription_duration: AddSubscriptionDuration,
        settings_dao: SettingsDao,
        bonus_dao: BonusDaysDao,
    ) -> None:
        super().__init__(
            uow,
            user_dao,
            subscription_dao,
            referral_dao,
            event_publisher,
            change_user_points,
            add_subscription_duration,
        )
        self.settings_dao = settings_dao
        self.bonus_dao = bonus_dao

    async def _execute(self, actor: UserDto, data: GiveReferrerRewardDto) -> None:
        reward = data.reward
        if reward.type != ReferralRewardType.EXTRA_DAYS or not (
            (await self.settings_dao.get()).extra.s.bonus.enabled
        ):
            return await super()._execute(actor, data)

        user = await self.user_dao.get_by_id(data.user_id)
        if not user:
            logger.warning(f"{actor.log} User '{data.user_id}' not found, reward not kept")
            return None

        # The reward is in the table already, not issued: it is kept as it is.
        total = await self.bonus_dao.pending(user.id)
        logger.info(f"{actor.log} Kept '{reward.amount}' bonus days for {user.log} ({total} kept)")
        await self.event_publisher.publish(
            BonusDaysKeptEvent(
                user=user,
                name=data.referred_name,
                value=reward.amount,
                reward_type=reward.type,
                total=total,
            )
        )
        return None


class BonusDays:
    def __init__(
        self,
        uow: UnitOfWork,
        bonus_dao: BonusDaysDao,
        settings_dao: SettingsDao,
        subscription_dao: SubscriptionDao,
        plan_dao: PlanDao,
        add_subscription_duration: AddSubscriptionDuration,
        set_user_subscription: SetUserSubscription,
    ) -> None:
        self.uow = uow
        self.bonus_dao = bonus_dao
        self.settings_dao = settings_dao
        self.subscription_dao = subscription_dao
        self.plan_dao = plan_dao
        self.add_subscription_duration = add_subscription_duration
        self.set_user_subscription = set_user_subscription

    async def enabled(self) -> bool:
        return (await self.settings_dao.get()).extra.s.bonus.enabled

    async def kept(self, user_id: int) -> int:
        return await self.bonus_dao.pending(user_id)

    async def take(self, user: UserDto) -> BonusDaysTaken:
        """All the days kept for ``user``, to the subscription or a new one."""
        async with self.uow:
            claimed = await self.bonus_dao.claim(user.id)
            await self.uow.commit()

        days = sum(claimed.values())
        if not days:
            raise BonusDaysError("none")

        try:
            await self._grant(user, days)
        except Exception:
            async with self.uow:
                await self.bonus_dao.release(list(claimed))
                await self.uow.commit()
            raise

        subscription = await self.subscription_dao.get_current(user.id)
        if subscription is None:
            raise RuntimeError(f"{user.log} has no subscription after taking bonus days")
        logger.info(f"{user.log} Took {days} bonus days, now until {subscription.expire_at}")
        return BonusDaysTaken(days=days, expire_at=subscription.expire_at)

    async def _grant(self, user: UserDto, days: int) -> None:
        subscription = await self.subscription_dao.get_current(user.id)
        if subscription and subscription.status != SubscriptionStatus.DELETED:
            if subscription.is_unlimited:
                # Days are no use to it; they stay kept.
                raise BonusDaysError("unlimited")
            await self.add_subscription_duration.system(
                AddSubscriptionDurationDto(user_id=user.id, days=days)
            )
            return

        plan_id = (await self.settings_dao.get()).extra.s.bonus.plan_id
        plan = await self.plan_dao.get_by_id(plan_id) if plan_id is not None else None
        if plan is None:
            raise BonusDaysError("no-plan")
        await self.set_user_subscription.system(
            SetUserSubscriptionDto(user_id=user.id, plan_id=plan.id, duration=days)
        )
