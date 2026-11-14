"""Giving a subscription for any number of days from the user card (docs/s/README.md).

Upstream gives a plan only for one of its own durations, and "add days" needs a
subscription already. The window where the duration is chosen also takes a
number typed in: the plan is given for that many days.
"""

from aiogram.types import Message
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.widgets.input import MessageInput
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common import Notifier
from src.application.dto import TelegramUserDto
from src.application.use_cases.subscription.commands.set_plan import (
    SetUserSubscription,
    SetUserSubscriptionDto,
)
from src.core.constants import TARGET_USER_ID, USER_KEY
from src.core.utils.validators import parse_int
from src.telegram.states import DashboardUser

MAX_DAYS = 3650


@inject
async def on_give_days_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    set_user_subscription: FromDishka[SetUserSubscription],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    days = parse_int(message.text)
    if days is None or not 0 < days <= MAX_DAYS:
        await notifier.notify_user(user, i18n_key="ntf-s-give.days-invalid")
        return

    await set_user_subscription(
        user,
        SetUserSubscriptionDto(
            user_id=dialog_manager.dialog_data[TARGET_USER_ID],
            plan_id=dialog_manager.dialog_data["selected_plan_id"],
            duration=days,
        ),
    )
    await dialog_manager.switch_to(state=DashboardUser.MAIN)
