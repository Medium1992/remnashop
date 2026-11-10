from typing import Any

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common import Notifier
from src.application.common.dao import SettingsDao
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.application.use_cases.s.settings import (
    SetSSetting,
    SetSSettingDto,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
    UpdateVersionChannelDto,
)
from src.core.constants import USER_KEY
from src.core.utils.client_versions import merge_sources, parse_source_input
from src.infrastructure.services.s.jobs import AdminAction, ReminderKind
from src.infrastructure.taskiq.tasks.s_devices import (
    s_device_admin_task,
    s_force_refresh_client_versions_task,
    s_refresh_client_channel_task,
)
from src.telegram.routers.dashboard.users.user.handlers import start_user_window
from src.telegram.s_states import SDashboard

from .getters import split_channel_id

# Button id -> the setting it flips. Ids cannot carry dots.
TOGGLES = {
    "slots_enabled": "slots.enabled",
    "versions_enabled": "versions.enabled",
    "versions_notify": "versions.notify_enabled",
    "versions_ignore_patch": "versions.ignore_patch",
    "stale_enabled": "stale.enabled",
    "inactive_enabled": "inactive.enabled",
    "inactive_include_disabled": "inactive.include_disabled",
    "invites_enabled": "invites.enabled",
    "plans_calendar": "plans.calendar_months",
}

# Button id -> (number setting, window to come back to).
NUMBERS = {
    "slots_base": ("slots.base_limit", SDashboard.SLOTS),
    "slots_ttl": ("slots.slot_ttl_minutes", SDashboard.SLOTS),
    "versions_interval": ("versions.interval_days", SDashboard.VERSIONS),
    "versions_refresh": ("versions.refresh_hours", SDashboard.VERSIONS),
    "versions_min_devices": ("versions.observed_min_devices", SDashboard.VERSIONS),
    "stale_hours": ("stale.stale_hours", SDashboard.STALE),
    "stale_interval": ("stale.interval_days", SDashboard.STALE),
    "inactive_days": ("inactive.delete_after_days", SDashboard.INACTIVE),
    "inactive_warn": ("inactive.warn_days", SDashboard.INACTIVE),
    "invites_ttl": ("invites.ttl_hours", SDashboard.INVITES),
    "invites_max": ("invites.max_active", SDashboard.INVITES),
}


def _user(dialog_manager: DialogManager) -> TelegramUserDto:
    return dialog_manager.middleware_data[USER_KEY]  # type: ignore[no-any-return]


async def _say(notifier: Notifier, user: TelegramUserDto, key: str, **kwargs: Any) -> None:
    await notifier.notify_user(user, payload=MessagePayloadDto(i18n_key=key, i18n_kwargs=kwargs))


@inject
async def on_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    toggle: FromDishka[ToggleSSetting],
) -> None:
    await toggle(_user(dialog_manager), TOGGLES[widget.widget_id])  # type: ignore[index]


async def on_number_request(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    path, back = NUMBERS[widget.widget_id]  # type: ignore[index]
    dialog_manager.dialog_data["s_field"] = path
    dialog_manager.dialog_data["s_back"] = back.state
    await dialog_manager.switch_to(SDashboard.NUMBER)


@inject
async def on_number_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    set_setting: FromDishka[SetSSetting],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user = _user(dialog_manager)
    path = dialog_manager.dialog_data.get("s_field", "")
    try:
        await set_setting(user, SetSSettingDto(path=path, raw_value=message.text or ""))
    except ValueError:
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    back = dialog_manager.dialog_data.pop("s_back", SDashboard.MAIN.state)
    dialog_manager.dialog_data.pop("s_field", None)
    await dialog_manager.switch_to(_state(back))


def _state(value: str) -> Any:
    for state in SDashboard.__states__:
        if state.state == value:
            return state
    return SDashboard.MAIN


async def on_back_from_number(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    back = dialog_manager.dialog_data.pop("s_back", SDashboard.MAIN.state)
    dialog_manager.dialog_data.pop("s_field", None)
    await dialog_manager.switch_to(_state(back))


# ── versions ─────────────────────────────────────────────────────────────────


@inject
async def on_refresh_all(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
) -> None:
    await s_force_refresh_client_versions_task.kiq()  # type: ignore[call-overload]
    await _say(notifier, _user(dialog_manager), "ntf-s-admin.refresh-started")


async def on_channel_select(
    callback: CallbackQuery, widget: Any, dialog_manager: DialogManager, item_id: str
) -> None:
    dialog_manager.dialog_data["s_channel"] = item_id
    await dialog_manager.switch_to(SDashboard.CHANNEL)


@inject
async def on_channel_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    update_channel: FromDishka[UpdateVersionChannel],
    settings_dao: FromDishka[SettingsDao],
) -> None:
    app, key = split_channel_id(dialog_manager.dialog_data["s_channel"])
    sources = merge_sources((await settings_dao.get()).extra.s.versions.sources)
    enabled = bool(sources[app]["channels"][key].get("enabled", True))
    await update_channel(
        _user(dialog_manager),
        UpdateVersionChannelDto(app=app, channel=key, patch={"enabled": not enabled}),
    )


@inject
async def on_channel_reset(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    update_channel: FromDishka[UpdateVersionChannel],
) -> None:
    app, key = split_channel_id(dialog_manager.dialog_data["s_channel"])
    await update_channel(
        _user(dialog_manager), UpdateVersionChannelDto(app=app, channel=key, patch=None)
    )
    await s_refresh_client_channel_task.kiq(app, key)  # type: ignore[call-overload]


@inject
async def on_channel_check(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
) -> None:
    app, key = split_channel_id(dialog_manager.dialog_data["s_channel"])
    await s_refresh_client_channel_task.kiq(app, key)  # type: ignore[call-overload]
    await _say(notifier, _user(dialog_manager), "ntf-s-admin.refresh-started")


@inject
async def on_channel_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    update_channel: FromDishka[UpdateVersionChannel],
) -> None:
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    user = _user(dialog_manager)
    parsed = parse_source_input(message.text or "")
    if parsed.patch is None:
        await _say(notifier, user, "ntf-s-admin.source-unknown")
        return
    app, key = split_channel_id(dialog_manager.dialog_data["s_channel"])
    await update_channel(user, UpdateVersionChannelDto(app=app, channel=key, patch=parsed.patch))
    await s_refresh_client_channel_task.kiq(app, key)  # type: ignore[call-overload]
    await _say(notifier, user, "ntf-s-admin.source-saved", kind=parsed.kind, value=parsed.value)


# ── reminders ────────────────────────────────────────────────────────────────


def _kind(dialog_manager: DialogManager) -> str:
    state = dialog_manager.current_context().state
    if state == SDashboard.VERSIONS:
        return ReminderKind.VERSIONS
    if state == SDashboard.STALE:
        return ReminderKind.STALE
    return str(dialog_manager.dialog_data.get("s_kind", ReminderKind.STALE))


@inject
async def on_summary(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
) -> None:
    user = _user(dialog_manager)
    await s_device_admin_task.kiq(  # type: ignore[call-overload]
        user.telegram_id, _kind(dialog_manager), AdminAction.SUMMARY, None
    )
    await _say(notifier, user, "ntf-s-admin.summary-started")


async def on_preview_request(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    dialog_manager.dialog_data["s_kind"] = _kind(dialog_manager)
    await dialog_manager.switch_to(SDashboard.TARGET)


@inject
async def on_target_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
) -> None:
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    user = _user(dialog_manager)
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return
    await s_device_admin_task.kiq(  # type: ignore[call-overload]
        user.telegram_id, dialog_manager.dialog_data["s_kind"], AdminAction.PREVIEW, int(raw)
    )
    await _say(notifier, user, "ntf-s-admin.preview-started")


async def on_send_request(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    dialog_manager.dialog_data["s_kind"] = _kind(dialog_manager)
    await dialog_manager.switch_to(SDashboard.CONFIRM_SEND)


@inject
async def on_send_confirm(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
) -> None:
    user = _user(dialog_manager)
    kind = dialog_manager.dialog_data["s_kind"]
    await s_device_admin_task.kiq(  # type: ignore[call-overload]
        user.telegram_id, kind, AdminAction.SEND, None
    )
    await _say(notifier, user, "ntf-s-admin.send-started")
    await dialog_manager.switch_to(
        SDashboard.VERSIONS if kind == ReminderKind.VERSIONS else SDashboard.STALE
    )


async def on_back_to_kind(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    kind = dialog_manager.dialog_data.get("s_kind")
    await dialog_manager.switch_to(
        SDashboard.VERSIONS if kind == ReminderKind.VERSIONS else SDashboard.STALE
    )


# ── routers ──────────────────────────────────────────────────────────────────


@inject
async def on_routers_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    update_routers: FromDishka[UpdateRouterHwids],
) -> None:
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    await update_routers(_user(dialog_manager), message.text or "")


# ── inactive ─────────────────────────────────────────────────────────────────


async def on_inactive_select(
    callback: CallbackQuery, widget: Any, dialog_manager: DialogManager, item_id: str
) -> None:
    await start_user_window(manager=dialog_manager, target_user_id=int(item_id))
