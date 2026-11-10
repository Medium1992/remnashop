"""Temporary blocks: off in the panel for a while, then back on by themselves.

A block disables the panel user, drops its live connections and tells the
person why and for how long. It is kept in ``s_user_blocks`` rather than in
Redis: losing it must not leave someone switched off forever. Buying or
renewing during a block does not lift it -- the panel client keeps a blocked
user DISABLED on every write.
"""

from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from src.application.common import Notifier
from src.application.common.dao import SubscriptionDao, UserDao
from src.application.common.policy import Permission, PermissionPolicy
from src.application.common.uow import UnitOfWork
from src.application.dto import MessagePayloadDto, UserDto
from src.application.dto.s_blocks import UserBlockDto
from src.core.enums import SubscriptionStatus
from src.core.exceptions import PermissionDeniedError
from src.core.utils.s_time import DATE_TIME, tg_time
from src.core.utils.time import datetime_now
from src.infrastructure.database.dao.s_blocks import UserBlockDao

from .remnawave import SRemnawave

UNTIL_FORMAT = "%d.%m.%Y %H:%M UTC"


def local_until(until: datetime) -> str:
    """The end of a block for a message: in the reader's time, UTC for old clients."""
    return tg_time(until, DATE_TIME, until.strftime(UNTIL_FORMAT))


DURATIONS_HOURS = (1, 6, 24, 24 * 7)


class BlockService:
    def __init__(
        self,
        uow: UnitOfWork,
        block_dao: UserBlockDao,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        remnawave: SRemnawave,
        notifier: Notifier,
    ) -> None:
        self.uow = uow
        self.block_dao = block_dao
        self.user_dao = user_dao
        self.subscription_dao = subscription_dao
        self.remnawave = remnawave
        self.notifier = notifier

    async def get(self, user_id: int) -> Optional[UserBlockDto]:
        return await self.block_dao.get(user_id)

    async def all(self) -> list[UserBlockDto]:
        return await self.block_dao.all()

    @staticmethod
    def ensure_allowed(actor: UserDto) -> None:
        if not PermissionPolicy.has_permission(actor, Permission.USER_SUBSCRIPTION_EDITOR):
            raise PermissionDeniedError()

    async def block(
        self, actor: UserDto, user_id: int, hours: Optional[int], reason: str = ""
    ) -> UserBlockDto:
        self.ensure_allowed(actor)
        user = await self.user_dao.get_by_id(user_id)
        if not user:
            raise ValueError(f"User '{user_id}' not found")
        block = UserBlockDto(
            user_id=user.id,
            telegram_id=user.telegram_id,
            reason=reason.strip()[:512],
            until=datetime_now() + timedelta(hours=hours) if hours else None,
            created_by=actor.telegram_id,
        )

        subscription = await self.subscription_dao.get_current(user.id)
        async with self.uow:
            if subscription and subscription.status != SubscriptionStatus.DELETED:
                await self.remnawave.disable_user(subscription.user_remna_id)
                await self.remnawave.drop_connections(subscription.user_remna_id)
                await self.subscription_dao.update_status(
                    subscription.id, SubscriptionStatus.DISABLED
                )
            await self.block_dao.put(block)
            await self.uow.commit()

        logger.warning(
            f"{actor.log} Blocked {user.log} "
            f"{'until ' + str(block.until) if block.until else 'until lifted'}: '{block.reason}'"
        )
        await self._tell(user, "ntf-s-blocks.blocked", block)
        return block

    async def unblock(self, actor: UserDto, user_id: int) -> bool:
        """Lift a block. False when there was none."""
        self.ensure_allowed(actor)
        user = await self.user_dao.get_by_id(user_id)
        block = await self.block_dao.get(user_id)
        if not user or not block:
            return False

        subscription = await self.subscription_dao.get_current(user.id)
        async with self.uow:
            # A subscription renewed or switched during the block is still
            # DISABLED: the panel client kept it so. Only that is switched on.
            if subscription and subscription.status == SubscriptionStatus.DISABLED:
                await self.remnawave.enable_user(subscription.user_remna_id)
                await self.subscription_dao.update_status(
                    subscription.id, SubscriptionStatus.ACTIVE
                )
            await self.block_dao.remove(user_id)
            await self.uow.commit()

        logger.info(f"{actor.log} Unblocked {user.log}")
        await self._tell(user, "ntf-s-blocks.unblocked", block)
        return True

    async def release_due(self, actor: UserDto) -> int:
        """Lift every block whose time is up. Returns how many were lifted."""
        released = 0
        for block in await self.block_dao.due(datetime_now()):
            try:
                if await self.unblock(actor, block.user_id):
                    released += 1
            except Exception as e:
                # The panel is down, say; the next run tries again.
                logger.warning(f"Block of user '{block.user_id}' not lifted yet: {e}")
        return released

    async def _tell(self, user: UserDto, key: str, block: UserBlockDto) -> None:
        try:
            await self.notifier.notify_user(
                user,
                payload=MessagePayloadDto(
                    i18n_key=key,
                    i18n_kwargs={
                        "reason": block.reason or "none",
                        "until": local_until(block.until) if block.until else "none",
                    },
                    disable_default_markup=False,
                    delete_after=None,
                ),
            )
        except Exception as e:
            logger.warning(f"Block notice to {user.log} failed: {e}")
