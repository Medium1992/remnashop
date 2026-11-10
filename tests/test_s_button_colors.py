"""Buttons go out blue unless they say otherwise, as in the bot before."""

from typing import Any

from aiogram.enums import ButtonStyle
from aiogram.methods import AnswerInlineQuery, EditMessageReplyMarkup, SendMessage, SendPhoto
from aiogram.types import (
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from src.telegram.s_button_colors import SButtonColors


def markup(*buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(buttons)])


async def sent(method: Any) -> Any:
    seen: list[Any] = []

    async def make_request(bot: Any, request: Any) -> None:
        seen.append(request)

    await SButtonColors()(make_request, None, method)  # type: ignore[arg-type]
    return seen[0]


def styles(markup: Any) -> list[Any]:
    return [b.style for row in markup.inline_keyboard for b in row]


PLAIN = InlineKeyboardButton(text="Назад", callback_data="back")
RED = InlineKeyboardButton(text="Удалить", callback_data="del", style=ButtonStyle.DANGER)


async def test_a_button_without_a_style_goes_out_blue() -> None:
    request = await sent(SendMessage(chat_id=1, text="x", reply_markup=markup(PLAIN, RED)))
    assert styles(request.reply_markup) == [ButtonStyle.PRIMARY, ButtonStyle.DANGER]


async def test_the_original_keyboard_is_left_alone() -> None:
    keyboard = markup(PLAIN)
    await sent(SendMessage(chat_id=1, text="x", reply_markup=keyboard))
    assert styles(keyboard) == [None]


async def test_screens_with_a_banner_and_edits_too() -> None:
    photo = await sent(SendPhoto(chat_id=1, photo="id", reply_markup=markup(PLAIN)))
    edit = await sent(EditMessageReplyMarkup(chat_id=1, message_id=1, reply_markup=markup(PLAIN)))
    assert styles(photo.reply_markup) == styles(edit.reply_markup) == [ButtonStyle.PRIMARY]


async def test_a_disabled_button_stays_grey() -> None:
    off = InlineKeyboardButton(text="3 из 3", disabled=DisabledButton())
    request = await sent(SendMessage(chat_id=1, text="x", reply_markup=markup(off)))
    assert styles(request.reply_markup) == [None]


async def test_the_inline_invite_button() -> None:
    result = InlineQueryResultArticle(
        id="1",
        title="t",
        input_message_content=InputTextMessageContent(message_text="m"),
        reply_markup=markup(PLAIN),
    )
    request = await sent(AnswerInlineQuery(inline_query_id="q", results=[result]))
    assert styles(request.results[0].reply_markup) == [ButtonStyle.PRIMARY]


async def test_other_keyboards_and_none_pass_untouched() -> None:
    reply = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="a")]])
    assert (await sent(SendMessage(chat_id=1, text="x", reply_markup=reply))).reply_markup is reply
    assert (await sent(SendMessage(chat_id=1, text="x"))).reply_markup is None
