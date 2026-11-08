"""Blocking one user for a while, opened from the user card."""

from typing import Any

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, ShowMode, StartMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from magic_filter import F

from src.application.common.dao import UserDao
from src.application.dto import TelegramUserDto
from src.core.constants import TARGET_USER_ID, USER_KEY
from src.core.enums import BannerName
from src.infrastructure.services.s.blocks import DURATIONS_HOURS, UNTIL_FORMAT, BlockService
from src.telegram.s_states import SBlock
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Cancel, Row
from src.telegram.window import Window

TARGET = "s_block_user_id"
REASON = "s_block_reason"


async def start_block_dialog(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    """From the user card: the target is already in its dialog data."""
    await dialog_manager.start(
        SBlock.MAIN, data={TARGET: dialog_manager.dialog_data[TARGET_USER_ID]}
    )


async def start_block_dialog_for(dialog_manager: DialogManager, user_id: int) -> None:
    await dialog_manager.start(SBlock.MAIN, data={TARGET: user_id}, mode=StartMode.NORMAL)


def _target(dialog_manager: DialogManager) -> int:
    return int(dialog_manager.start_data[TARGET])  # type: ignore[index, call-overload]


@inject
async def block_getter(
    dialog_manager: DialogManager,
    blocks: FromDishka[BlockService],
    user_dao: FromDishka[UserDao],
    **kwargs: Any,
) -> dict[str, Any]:
    target = await user_dao.get_by_id(_target(dialog_manager))
    block = await blocks.get(_target(dialog_manager))
    return {
        "name": target.name if target else "?",
        "telegram_id": (target.telegram_id if target else None) or 0,
        "blocked": int(block is not None),
        "until": block.until.strftime(UNTIL_FORMAT) if block and block.until else "none",
        "reason": (block.reason if block else dialog_manager.dialog_data.get(REASON, "")) or "none",
    }


@inject
async def on_duration(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    blocks: FromDishka[BlockService],
) -> None:
    actor: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    hours = int(widget.widget_id.removeprefix("h")) or None  # type: ignore[union-attr]
    await blocks.block(
        actor, _target(dialog_manager), hours, dialog_manager.dialog_data.get(REASON, "")
    )


@inject
async def on_unblock(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    blocks: FromDishka[BlockService],
) -> None:
    actor: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await blocks.unblock(actor, _target(dialog_manager))


async def on_reason(message: Message, widget: MessageInput, dialog_manager: DialogManager) -> None:
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    dialog_manager.dialog_data[REASON] = (message.text or "").strip()[:512]


def _duration(hours: int) -> Button:
    return Button(
        I18nFormat("btn-s-block.duration", value=hours), id=f"h{hours}", on_click=on_duration
    )


main = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-block.main"),
    Row(*(_duration(h) for h in DURATIONS_HOURS[:2])),
    Row(*(_duration(h) for h in DURATIONS_HOURS[2:])),
    Row(_duration(0)),
    Row(
        Button(
            I18nFormat("btn-s-block.unblock"), id="unblock", on_click=on_unblock, when=F["blocked"]
        )
    ),
    Row(Cancel(I18nFormat("btn-back.general"), id="back")),
    MessageInput(func=on_reason),
    IgnoreUpdate(),
    state=SBlock.MAIN,
    getter=block_getter,
)

router = Dialog(main)
