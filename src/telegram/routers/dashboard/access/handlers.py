from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.utils import remove_intent_id
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Select
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.dto import TelegramUserDto
from src.application.use_cases.settings.commands.access import (
    ChangeAccessMode,
    TogglePayments,
    ToggleRegistration,
)
from src.application.use_cases.settings.commands.requirements import (
    ToggleConditionRequirement,
    UpdateChannelRequirement,
    UpdateRulesRequirement,
)
from src.core.constants import USER_KEY
from src.core.enums import AccessMode, AccessRequirements
from src.telegram.routers.s.rules import is_rules_text, on_rules_text_input
from src.telegram.states import DashboardAccess


@inject
async def on_access_mode_select(
    callback: CallbackQuery,
    widget: Select,
    dialog_manager: DialogManager,
    selected_mode: AccessMode,
    change_access_mode: FromDishka[ChangeAccessMode],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await change_access_mode(user, selected_mode)


@inject
async def on_payments_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    toggle_payments: FromDishka[TogglePayments],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await toggle_payments(user)


@inject
async def on_registration_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    toggle_registration: FromDishka[ToggleRegistration],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await toggle_registration(user)


@inject
async def on_condition_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    toggle_condition_requirement: FromDishka[ToggleConditionRequirement],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    try:
        condition = AccessRequirements(remove_intent_id(callback.data or "")[-1])
    except ValueError:
        logger.warning(f"{user.log} Unrecognized condition toggle payload '{callback.data}'")
        return
    await toggle_condition_requirement(user, condition)


@inject
async def on_rules_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    update_rules_requirement: FromDishka[UpdateRulesRequirement],
) -> None:
    # s edition: anything but a link is the rules text itself.
    if is_rules_text(message):
        await on_rules_text_input(message, dialog_manager)
        return

    dialog_manager.show_mode = ShowMode.EDIT
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]

    if await update_rules_requirement(user, message.text or ""):
        await dialog_manager.switch_to(state=DashboardAccess.CONDITIONS)


@inject
async def on_channel_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    update_channel_requirement: FromDishka[UpdateChannelRequirement],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await update_channel_requirement(user, message.text or "")
