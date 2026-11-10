"""Every inline button blue unless it says otherwise (Bot API 9.4, ``style``).

As in the bot this one replaced: blue is the default, green and red are set
where the action calls for them. Done on the way out, so every screen,
notice and inline result gets it without each keyboard spelling it out.
"""

from typing import Any, Optional

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.enums import ButtonStyle
from aiogram.methods import AnswerInlineQuery, TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

DEFAULT_STYLE = ButtonStyle.PRIMARY


def _colored(button: InlineKeyboardButton) -> InlineKeyboardButton:
    # A disabled button stays grey: that is how it shows it does nothing.
    if button.style is not None or button.disabled is not None:
        return button
    return button.model_copy(update={"style": DEFAULT_STYLE})


def color_markup(markup: Any) -> Any:
    """``markup`` with the default style put in; anything else as it is."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows = [[_colored(b) for b in row] for row in markup.inline_keyboard]
    return markup.model_copy(update={"inline_keyboard": rows})


def _with_markup(item: Any) -> Any:
    markup: Optional[Any] = getattr(item, "reply_markup", None)
    if markup is None:
        return item
    colored = color_markup(markup)
    return item if colored is markup else item.model_copy(update={"reply_markup": colored})


class SButtonColors(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        colored: Any
        if isinstance(method, AnswerInlineQuery):
            results = [_with_markup(r) for r in method.results]
            colored = method.model_copy(update={"results": results})
        else:
            colored = _with_markup(method)
        return await make_request(bot, colored)
