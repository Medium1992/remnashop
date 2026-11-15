"""Hand-set values of a subscription kept on renewal (docs/s/README.md).

A renewal takes the device limit, the traffic limit and the squads from the
plan again, so what an admin set by hand in the user card is lost. With the
feature on, the admin marks, per subscription, which of the three stay: the
renewal leaves them as they are. A plan change (and a plan given by an admin)
makes a new subscription, which keeps nothing; the admins are told when
something kept was dropped that way.
"""

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from src.application.common import EventPublisher
from src.application.common.dao import SettingsDao, SubscriptionDao
from src.application.common.uow import UnitOfWork
from src.application.dto import PlanSnapshotDto, SubscriptionDto, UserDto
from src.application.dto.s_kept import KeptLimitsDto
from src.application.events.system import UserEvent
from src.core.enums import SystemNotificationType
from src.core.types import NotificationType
from src.infrastructure.database.dao.s_kept import KeptLimitsDao


def renew_from_plan(
    subscription: SubscriptionDto, plan: PlanSnapshotDto, kept: KeptLimitsDto
) -> None:
    """What a renewal takes from the plan, but for what is kept."""
    if not kept.devices:
        subscription.device_limit = plan.device_limit
    if not kept.traffic:
        subscription.traffic_limit = plan.traffic_limit
        subscription.traffic_limit_strategy = plan.traffic_limit_strategy
    subscription.tag = plan.tag
    if not kept.squads:
        subscription.internal_squads = plan.internal_squads
        subscription.external_squad = plan.external_squad


@dataclass(frozen=True, kw_only=True)
class KeptLimitsDroppedEvent(UserEvent):
    """A plan change dropped what an admin had kept for the subscription."""

    notification_type: NotificationType = field(
        default=SystemNotificationType.SUBSCRIPTION, init=False
    )
    devices: int = 0
    traffic: int = 0
    squads: int = 0
    device_limit: int = 0
    traffic_limit: int = 0

    @property
    def event_key(self) -> str:
        return "event-s-kept.dropped"


class KeptLimits:
    def __init__(
        self,
        uow: UnitOfWork,
        dao: KeptLimitsDao,
        settings_dao: SettingsDao,
        subscription_dao: SubscriptionDao,
        event_publisher: EventPublisher,
    ) -> None:
        self.uow = uow
        self.dao = dao
        self.settings_dao = settings_dao
        self.subscription_dao = subscription_dao
        self.event_publisher = event_publisher

    async def enabled(self) -> bool:
        return (await self.settings_dao.get()).extra.s.kept.enabled

    async def of(self, subscription: Optional[SubscriptionDto]) -> KeptLimitsDto:
        """What stays on renewal; nothing while the feature is off.

        Read in the middle of a paid renewal: on any failure nothing is kept, and
        the renewal goes on taking all from the plan, as upstream does.
        """
        if subscription is None or subscription.id is None:
            return KeptLimitsDto()
        try:
            if not await self.enabled():
                return KeptLimitsDto()
            return await self.dao.get(subscription.id)
        except Exception as error:
            logger.exception(f"Kept values of '{subscription.id}' not read, plan used: {error}")
            return KeptLimitsDto()

    async def toggle(self, user_id: int, name: str) -> Optional[bool]:
        """Flips ``name`` for the user's subscription; None when there is none."""
        async with self.uow:
            subscription = await self.subscription_dao.get_current(user_id)
            if subscription is None or subscription.id is None:
                return None
            value = not getattr(await self.dao.get(subscription.id), name)
            await self.dao.set(subscription.id, name, value)
            await self.uow.commit()
        logger.info(f"Kept '{name}' of subscription '{subscription.id}' set to {value}")
        return value

    async def dropped(self, user: UserDto, old: SubscriptionDto) -> None:
        """Tells the admins what ``old`` kept, now that a new plan replaced it.

        Called once the plan change is committed: a failure here is only logged.
        """
        try:
            await self._dropped(user, old)
        except Exception as error:
            logger.exception(f"{user.log} Dropped kept values not told: {error}")

    async def _dropped(self, user: UserDto, old: SubscriptionDto) -> None:
        kept = await self.of(old)
        if not kept.any:
            return
        logger.info(f"{user.log} Plan change dropped the kept values of '{old.id}': {kept}")
        await self.event_publisher.publish(
            KeptLimitsDroppedEvent(
                user_id=user.id,
                telegram_id=user.telegram_id,
                username=user.username,
                name=user.name,
                devices=int(kept.devices),
                traffic=int(kept.traffic),
                squads=int(kept.squads),
                device_limit=old.device_limit,
                traffic_limit=old.traffic_limit,
            )
        )
