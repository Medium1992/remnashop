"""Buttons under the torrent blocker report: block right from the alert."""

from typing import Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_dialog import DialogManager, StartMode
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import TranslatorRunner
from src.application.dto import TelegramUserDto
from src.core.exceptions import PermissionDeniedError
from src.infrastructure.services.s.blocks import BlockService
from src.telegram.keyboards import get_user_keyboard
from src.telegram.s_states import SBlock

from .dialog import TARGET

PREFIX = "s_block:"
MENU = "menu"
QUICK_HOURS = 24

router = Router(name=__name__)


def torrent_report_keyboard(user_id: Optional[int]) -> Optional[InlineKeyboardMarkup]:
    """The user card, plus blocking for a day or for another term."""
    card = get_user_keyboard(user_id)
    if not user_id or card is None:
        return card
    builder = InlineKeyboardBuilder.from_markup(card)
    builder.row(
        InlineKeyboardButton(
            text="btn-s-block.quick", callback_data=f"{PREFIX}{user_id}:{QUICK_HOURS}"
        ),
        InlineKeyboardButton(text="btn-s-block.other", callback_data=f"{PREFIX}{user_id}:{MENU}"),
    )
    return builder.as_markup()


@router.callback_query(F.data.startswith(PREFIX))
@inject
async def on_block_button(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    blocks: FromDishka[BlockService],
    i18n: FromDishka[TranslatorRunner],
) -> None:
    raw_user_id, _, action = (callback.data or "").removeprefix(PREFIX).partition(":")
    try:
        user_id = int(raw_user_id)
    except ValueError:
        await callback.answer()
        return

    if action == MENU:
        await dialog_manager.start(SBlock.MAIN, data={TARGET: user_id}, mode=StartMode.RESET_STACK)
        await callback.answer()
        return

    try:
        await blocks.block(
            user, user_id, int(action), reason=i18n.get("msg-s-block.torrent-reason")
        )
    except PermissionDeniedError:
        logger.warning(f"{user.log} tried to block '{user_id}' without the permission")
        await callback.answer()
        return
    await callback.answer(i18n.get("ntf-s-blocks.done"), show_alert=False)
