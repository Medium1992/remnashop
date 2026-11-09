"""One-time invite links at ``/start`` (docs/s/README.md).

Runs twice, around the upstream access check:

- ``PEEK`` (before ``AccessMiddleware``): a live token is replaced by its
  owner's referral code, so the access check and registration see an ordinary
  referral. A dead token, or a permanent code while one-time links are on,
  is replaced by nothing — the visitor arrives as if without a link.
- ``SPEND`` (after access is granted, before ``UserMiddleware`` registers the
  user): the token is spent. Only a new user spends it: an existing user can
  not be referred anyway, and his click must not burn a friend's link.

The replacement reaches upstream through ``parse_referral_code(event, data)``.
"""

from enum import Enum
from typing import Any, Awaitable, Callable, Final, Optional

from aiogram.types import Message, TelegramObject
from aiogram.types import User as AiogramUser
from dishka import AsyncContainer
from loguru import logger

from src.application.common import Notifier
from src.application.common.dao import SettingsDao, UserDao
from src.application.dto import TempUserDto
from src.core.constants import CONTAINER_KEY
from src.core.enums import AccessMode, MiddlewareEventType
from src.infrastructure.services.s.invites import InviteLinks, is_token

from ._codes import parse_referral_code
from .base import EventTypedMiddleware

# What upstream reads instead of the code in /start: a referral code, or "" for none.
REFERRAL_OVERRIDE_KEY: Final[str] = "s_referral_code"
_TOKEN_KEY: Final[str] = "s_invite_token"
_DEAD_KEY: Final[str] = "s_invite_dead"


class Stage(Enum):
    PEEK = "peek"
    SPEND = "spend"


class SInviteMiddleware(EventTypedMiddleware):
    __event_types__ = [MiddlewareEventType.MESSAGE]

    def __init__(self, stage: Stage) -> None:
        super().__init__()
        self.stage = stage

    async def middleware_logic(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        aiogram_user: Optional[AiogramUser] = self._get_aiogram_user(data)
        if not isinstance(event, Message) or aiogram_user is None or aiogram_user.is_bot:
            return await handler(event, data)

        if self.stage is Stage.PEEK:
            raw = parse_referral_code(event)
            if raw:
                await self._peek(raw, data)
            return await handler(event, data)

        if await self._spend(aiogram_user, data):
            return await handler(event, data)
        return None

    async def _peek(self, raw: str, data: dict[str, Any]) -> None:
        container: AsyncContainer = data[CONTAINER_KEY]
        invites = await container.get(InviteLinks)
        if not (await invites.settings()).enabled:
            return

        owner = await invites.owner(raw) if is_token(raw) else None
        if owner:
            data[REFERRAL_OVERRIDE_KEY] = owner.referral_code
            data[_TOKEN_KEY] = raw
            return

        # Expired, used, or a permanent code: no referral from this link.
        data[REFERRAL_OVERRIDE_KEY] = ""
        data[_DEAD_KEY] = True
        logger.info(f"Invite link '{raw}' is not a live one-time link")

    async def _spend(self, aiogram_user: AiogramUser, data: dict[str, Any]) -> bool:
        """Spend the token for a new user; False stops the update."""
        token: Optional[str] = data.get(_TOKEN_KEY)
        dead: bool = data.get(_DEAD_KEY, False)
        if not token and not dead:
            return True

        container: AsyncContainer = data[CONTAINER_KEY]
        user_dao = await container.get(UserDao)
        if await user_dao.get_by_telegram_id(aiogram_user.id):
            return True

        if token:
            invites = await container.get(InviteLinks)
            if await invites.spend(token):
                return True
            # Someone else used it between the access check and now.
            data[REFERRAL_OVERRIDE_KEY] = ""
            logger.info(f"Invite link '{token}' was used by someone else meanwhile")

        notifier = await container.get(Notifier)
        await notifier.notify_user(
            TempUserDto.from_aiogram(aiogram_user), i18n_key="ntf-s-invites.dead"
        )

        settings_dao: SettingsDao = await container.get(SettingsDao)
        settings = await settings_dao.get()
        # Invite-only bot: without a live link there is no way in. A dead link
        # was already turned away by the access check; this is the lost race.
        return settings.access.mode != AccessMode.INVITED
