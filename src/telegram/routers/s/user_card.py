"""Additions to the admin's user card (docs/s/README.md): the user's
receipts, and the card of whoever invited them."""

from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common.dao import ReferralDao
from src.core.constants import TARGET_USER_ID
from src.telegram.routers.dashboard.users.user.handlers import start_user_window


@inject
async def inviter_getter(
    dialog_manager: DialogManager,
    referral_dao: FromDishka[ReferralDao],
    **kwargs: Any,
) -> dict[str, Any]:
    referral = await referral_dao.get_by_referred_id(dialog_manager.dialog_data[TARGET_USER_ID])
    return {"s_inviter": int(referral is not None and referral.referrer is not None)}


@inject
async def on_open_inviter(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    referral_dao: FromDishka[ReferralDao],
) -> None:
    referral = await referral_dao.get_by_referred_id(dialog_manager.dialog_data[TARGET_USER_ID])
    if referral is None or referral.referrer is None:
        return
    await start_user_window(manager=dialog_manager, target_user_id=referral.referrer.id)
