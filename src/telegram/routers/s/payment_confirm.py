""" "✅ Провести оплату" in the admin's transaction card (docs/s/README.md).

Two taps: the first asks to check the gateway's cabinet, the second confirms.
Shown to those with the S dashboard (OWNER, DEV) for an order the bot never
saw paid; see application/use_cases/s/payments.py.
"""

from typing import Any
from uuid import UUID

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger
from magic_filter import F

from src.application.common import Notifier
from src.application.common.dao import TransactionDao
from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import TelegramUserDto
from src.application.use_cases.s.payments import (
    ConfirmOutcome,
    ConfirmPaymentByHand,
    can_confirm,
)
from src.core.constants import USER_KEY
from src.telegram.widgets import I18nFormat
from src.telegram.widgets.kbd import Button, Row

ASKED = "s_confirm_asked"


def _allowed(user: TelegramUserDto) -> bool:
    return PermissionPolicy.has_permission(user, Permission.SETTINGS_EXTRA)


@inject
async def confirm_getter(
    dialog_manager: DialogManager,
    transaction_dao: FromDishka[TransactionDao],
    **kwargs: Any,
) -> dict[str, Any]:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    payment_id = dialog_manager.dialog_data.get("selected_transaction")
    transaction = await transaction_dao.get_by_payment_id(UUID(str(payment_id)))
    shown = bool(transaction and _allowed(user) and can_confirm(transaction))
    asked = shown and dialog_manager.dialog_data.get(ASKED) == str(payment_id)
    return {"s_can_confirm": int(shown and not asked), "s_confirm_asked": int(asked)}


async def on_ask(callback: CallbackQuery, widget: Any, dialog_manager: DialogManager) -> None:
    dialog_manager.dialog_data[ASKED] = str(dialog_manager.dialog_data["selected_transaction"])


async def on_cancel(callback: CallbackQuery, widget: Any, dialog_manager: DialogManager) -> None:
    dialog_manager.dialog_data.pop(ASKED, None)


@inject
async def on_confirm(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    confirm: FromDishka[ConfirmPaymentByHand],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    payment_id = str(dialog_manager.dialog_data["selected_transaction"])
    if dialog_manager.dialog_data.pop(ASKED, None) != payment_id or not _allowed(user):
        return
    try:
        outcome = await confirm(user, UUID(payment_id))
    except Exception:
        logger.exception(f"{user.log} Payment '{payment_id}' not confirmed by hand")
        outcome = ConfirmOutcome.FAILED
    await notifier.notify_user(user, i18n_key=f"ntf-s-payment.{outcome.value.replace('_', '-')}")


def confirm_buttons() -> list[Row]:
    return [
        Row(
            Button(
                I18nFormat("btn-s-payment.confirm"),
                id="s_payment_ask",
                on_click=on_ask,
                when=F["s_can_confirm"],
            ),
        ),
        Row(
            Button(
                I18nFormat("btn-s-payment.confirm-sure"),
                id="s_payment_confirm",
                on_click=on_confirm,
                when=F["s_confirm_asked"],
            ),
            Button(
                I18nFormat("btn-s-payment.confirm-cancel"),
                id="s_payment_cancel",
                on_click=on_cancel,
                when=F["s_confirm_asked"],
            ),
        ),
    ]
