from typing import Any, Awaitable, Callable, Optional

from aiogram.types import ChatMemberUpdated, TelegramObject
from aiogram.types import User as AiogramUser
from aiogram_dialog.api.internal import FakeUser
from dishka import AsyncContainer
from loguru import logger

from src.application.dto import TelegramUserDto
from src.application.use_cases.user.commands.activity import TrackUserActivity
from src.application.use_cases.user.commands.registration import (
    GetOrCreateUser,
    GetOrCreateUserDto,
    UpdateUserProfile,
    UpdateUserProfileDto,
)
from src.core.constants import CONTAINER_KEY, USER_KEY
from src.core.enums import MiddlewareEventType

from ._codes import parse_ad_link_code, parse_referral_code
from .base import EventTypedMiddleware


class UserMiddleware(EventTypedMiddleware):
    __event_types__ = [
        MiddlewareEventType.MESSAGE,
        MiddlewareEventType.CALLBACK_QUERY,
        MiddlewareEventType.ERROR,
        MiddlewareEventType.AIOGD_UPDATE,
        MiddlewareEventType.MY_CHAT_MEMBER,
        MiddlewareEventType.PRE_CHECKOUT_QUERY,
    ]

    async def middleware_logic(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        aiogram_user: Optional[AiogramUser] = self._get_aiogram_user(data)

        if aiogram_user is None or aiogram_user.is_bot:
            logger.warning("Terminating middleware: event from bot or missing user")
            return

        is_chat_member_event = isinstance(event, ChatMemberUpdated)
        referral_code = parse_referral_code(event, data)
        ad_link_code = parse_ad_link_code(event)

        container: AsyncContainer = data[CONTAINER_KEY]
        get_or_create_user = await container.get(GetOrCreateUser)
        update_user_profile = await container.get(UpdateUserProfile)
        track_user_activity = await container.get(TrackUserActivity)

        user = await get_or_create_user.system(
            GetOrCreateUserDto(
                telegram_id=aiogram_user.id,
                username=aiogram_user.username,
                full_name=aiogram_user.full_name,
                language_code=aiogram_user.language_code,
                is_chat_member_event=is_chat_member_event,
                referral_code=referral_code,
                ad_link_code=ad_link_code,
            )
        )

        if user and not isinstance(aiogram_user, FakeUser):
            user = await update_user_profile.system(
                UpdateUserProfileDto(
                    user=user,
                    username=aiogram_user.username,
                    full_name=aiogram_user.full_name,
                    language_code=aiogram_user.language_code,
                    telegram_id=aiogram_user.id,
                )
            )
            await track_user_activity.system(user.id)

        if user is not None and not isinstance(user, TelegramUserDto):
            user = TelegramUserDto.from_user(user)
        data[USER_KEY] = user
        return await handler(event, data)
