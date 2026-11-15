"""Hand-set values kept on renewal, in the user card and for the user (docs/s/README.md)."""

from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button as ButtonWidget
from dishka import AsyncContainer
from dishka.integrations.aiogram import CONTAINER_NAME
from magic_filter import F

from src.application.common import Notifier
from src.application.common.dao import SubscriptionDao
from src.application.dto import TelegramUserDto
from src.application.dto.s_kept import KEPT_FIELDS
from src.application.services.s_kept import KeptLimits
from src.core.constants import TARGET_USER_ID, USER_KEY
from src.telegram.widgets import I18nFormat
from src.telegram.widgets.kbd import Button, Row

PREFIX = "s_kept_"


async def _kept(dialog_manager: DialogManager, user_id: int) -> dict[str, Any]:
    container: AsyncContainer = dialog_manager.middleware_data[CONTAINER_NAME]
    kept_limits = await container.get(KeptLimits)
    if not await kept_limits.enabled():
        return {"s_kept_on": 0, "s_kept_any": 0}
    subscription = await (await container.get(SubscriptionDao)).get_current(user_id)
    kept = await kept_limits.of(subscription)
    return {
        "s_kept_on": int(subscription is not None),
        "s_kept_any": int(kept.any),
        **{f"{PREFIX}{name}": int(getattr(kept, name)) for name in KEPT_FIELDS},
    }


async def kept_admin_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    """For the user card: the subscription of the user it shows."""
    return await _kept(dialog_manager, dialog_manager.dialog_data[TARGET_USER_ID])


async def kept_user_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    """For the user: their own subscription."""
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    return await _kept(dialog_manager, user.id)


async def on_kept_toggle(
    callback: CallbackQuery, widget: ButtonWidget, dialog_manager: DialogManager
) -> None:
    container: AsyncContainer = dialog_manager.middleware_data[CONTAINER_NAME]
    name = widget.widget_id.removeprefix(PREFIX)  # type: ignore[union-attr]
    value = await (await container.get(KeptLimits)).toggle(
        dialog_manager.dialog_data[TARGET_USER_ID], name
    )
    if value is None:
        admin: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
        await (await container.get(Notifier)).notify_user(admin, i18n_key="ntf-s-kept.none")


def kept_toggle(name: str) -> Row:
    """The button of the user card that keeps ``name`` on renewal, or not."""
    return Row(
        Button(
            I18nFormat("btn-s-kept.toggle", kept=F[f"{PREFIX}{name}"]),
            id=f"{PREFIX}{name}",
            on_click=on_kept_toggle,
        ),
        when=F["s_kept_on"],
    )
