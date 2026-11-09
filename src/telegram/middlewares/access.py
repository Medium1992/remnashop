from typing import Any, Awaitable, Callable, Optional

from aiogram.types import CallbackQuery, TelegramObject
from aiogram.types import User as AiogramUser
from aiogram_dialog.utils import remove_intent_id
from dishka import AsyncContainer
from loguru import logger

from src.application.dto import TempUserDto
from src.application.use_cases.access.queries.availability import CheckAccess, CheckAccessDto
from src.application.use_cases.ad_link.queries.validate import ValidateAdLinkCode
from src.application.use_cases.referral.queries.code import (
    ValidateReferralCode,
    ValidateReferralCodeDto,
)
from src.core.constants import CONTAINER_KEY, PAYMENT_PREFIX
from src.core.enums import MiddlewareEventType

from ._codes import parse_ad_link_code, parse_referral_code
from .base import EventTypedMiddleware


class AccessMiddleware(EventTypedMiddleware):
    __event_types__ = [MiddlewareEventType.MESSAGE, MiddlewareEventType.CALLBACK_QUERY]

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

        container: AsyncContainer = data[CONTAINER_KEY]
        check_access = await container.get(CheckAccess)

        raw_referral_code = parse_referral_code(event, data)
        is_referral_event = False
        if raw_referral_code:
            validate_referral_code = await container.get(ValidateReferralCode)
            # ValidateReferralCode.system uses SYSTEM_ACTOR (id=-1).
            # For the access check we only need to know whether a valid referral
            # code was present — the actual attachment happens later in
            # GetOrCreateUser.  We pass id=0 as a sentinel that prevents
            # the self-referral guard from triggering here; the guard will fire
            # again with the real user id in AttachReferral.
            is_referral_event = await validate_referral_code.system(
                ValidateReferralCodeDto(user_id=0, referral_code=raw_referral_code)
            )

        raw_ad_link_code = parse_ad_link_code(event)
        is_ad_link_event = False
        if raw_ad_link_code:
            validate_ad_link = await container.get(ValidateAdLinkCode)
            is_ad_link_event = await validate_ad_link.system(raw_ad_link_code)

        if await check_access.system(
            CheckAccessDto(
                temp_user=TempUserDto.from_aiogram(aiogram_user),
                is_payment_event=self._is_payment_event(event),
                is_referral_event=is_referral_event,
                is_ad_link_event=is_ad_link_event,
            )
        ):
            return await handler(event, data)

    def _is_payment_event(self, event: TelegramObject) -> bool:
        if not isinstance(event, CallbackQuery) or not event.data:
            return False

        callback_data = remove_intent_id(event.data)

        if callback_data[-1].startswith(PAYMENT_PREFIX):
            logger.debug(f"Detected payment event '{callback_data}'")
            return True

        return False
