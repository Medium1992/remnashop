from typing import Any, Awaitable, Callable

from aiogram.types import CallbackQuery, TelegramObject
from dishka import AsyncContainer

from src.application.common import Notifier
from src.application.common.dao import SettingsDao
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.application.use_cases.access.commands.validation import AcceptRules
from src.application.use_cases.access.queries.requirements import CheckRules
from src.core.constants import CONTAINER_KEY, USER_KEY
from src.core.enums import MiddlewareEventType
from src.telegram.keyboards import CALLBACK_RULES_ACCEPT, get_rules_keyboard
from src.telegram.s_rules import rules_payload, rules_text

from .base import EventTypedMiddleware


class RulesMiddleware(EventTypedMiddleware):
    __event_types__ = [MiddlewareEventType.MESSAGE, MiddlewareEventType.CALLBACK_QUERY]

    async def middleware_logic(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        container: AsyncContainer = data[CONTAINER_KEY]
        user: TelegramUserDto = data[USER_KEY]

        check_rules = await container.get(CheckRules)
        accept_rules = await container.get(AcceptRules)
        notifier = await container.get(Notifier)

        result = await check_rules(user)

        if not result.is_required:
            return await handler(event, data)

        if self._is_click_accept(event):
            await accept_rules(user)
            await self._delete_previous_message(event)
            return await handler(event, data)

        if not result.is_accepted:
            # s edition: the rules themselves when the admin wrote them.
            settings = await (await container.get(SettingsDao)).get()
            if rules_text(settings):
                payload = rules_payload(
                    rules_text(settings), accept=True, reply_markup=get_rules_keyboard()
                )
                await notifier.notify_user(user=user, payload=payload)
                return
            await notifier.notify_user(
                user=user,
                payload=MessagePayloadDto(
                    i18n_key="ntf-requirement.rules-accept-required",
                    i18n_kwargs={"url": result.rules_url},
                    reply_markup=get_rules_keyboard(),
                    delete_after=None,
                ),
            )
            return

        return await handler(event, data)

    def _is_click_accept(self, event: TelegramObject) -> bool:
        return isinstance(event, CallbackQuery) and event.data == CALLBACK_RULES_ACCEPT
