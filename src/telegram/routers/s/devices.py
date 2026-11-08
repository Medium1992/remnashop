"""The "s" additions to the user's own devices screen.

Hooked into the upstream screen at two points only: its getter merges
``devices_extra`` into what it returns, and its window shows ``s_devices`` and
the add-slot button.
"""

from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import AsyncContainer, FromDishka
from dishka.integrations.aiogram import CONTAINER_NAME
from dishka.integrations.aiogram_dialog import inject
from redis.asyncio import Redis
from remnapy.models.hwid import HwidDeviceDto

from src.application.common import Notifier, TranslatorRunner
from src.application.common.dao import SubscriptionDao
from src.application.dto import MessagePayloadDto, SettingsDto, SubscriptionDto, TelegramUserDto
from src.core.constants import USER_KEY
from src.core.utils.client_versions import (
    Sources,
    VersionsCache,
    app_title,
    check_install,
    merge_sources,
    parse_client,
)
from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.infrastructure.services.s.device_scan import is_router
from src.infrastructure.services.s.reminders import device_title, subscription_age
from src.infrastructure.services.s.slot_grants import read_grant, shown_counters
from src.infrastructure.services.s.slots import SlotOutcome, SlotService

ICON_FRESH = "✅"
ICON_OUTDATED = "⚠️"
ICON_STALE = "⏳"


async def devices_extra(
    dialog_manager: DialogManager,
    i18n: TranslatorRunner,
    settings: SettingsDto,
    subscription: SubscriptionDto,
    devices: list[HwidDeviceDto],
    formatted: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keys for the devices window; also prefixes the device buttons in place."""
    s = settings.extra.s
    container: AsyncContainer = dialog_manager.middleware_data[CONTAINER_NAME]
    extra: dict[str, Any] = {"s_slot_button": 0}
    lines: list[str] = []
    icons: dict[int, str] = {}

    if s.slots.enabled and subscription.device_limit > 0:
        occupied, shown = shown_counters(subscription.device_limit, len(devices))
        extra["max_count"] = shown
        extra["s_slot_button"] = int(occupied < subscription.device_limit)
        grant = await read_grant(await container.get(Redis), subscription.user_remna_id)
        if grant and grant.is_open:
            lines.append(i18n.get("msg-s-devices.slot-open", seconds=grant.seconds_left))
        elif extra["s_slot_button"]:
            lines.append(i18n.get("msg-s-devices.slot-hint", value=s.slots.slot_ttl_minutes))

    own = [(i, d) for i, d in enumerate(devices) if not is_router(d, s.router_hwids)]
    if s.versions.enabled:
        versions: ClientVersionsService = await container.get(ClientVersionsService)
        cache = await versions.load()
        lines += _version_lines(i18n, own, cache, merge_sources(s.versions.sources), icons)
    if s.stale.enabled:
        lines += _stale_lines(i18n, own, s.stale.stale_hours, icons)

    for index, icon in icons.items():
        if index < len(formatted):
            formatted[index]["label"] = f"{icon} {formatted[index]['label']}"

    extra["s_devices"] = "\n".join(lines)
    return extra


def _version_lines(
    i18n: TranslatorRunner,
    devices: list[tuple[int, HwidDeviceDto]],
    cache: VersionsCache,
    sources: Sources,
    icons: dict[int, str],
) -> list[str]:
    lines = []
    for index, device in devices:
        info = parse_client(device.user_agent, device.platform)
        verdict = check_install(info, cache, sources) if info else None
        if verdict is None:
            continue
        icons[index] = ICON_OUTDATED if verdict.outdated else ICON_FRESH
        if verdict.outdated:
            lines.append(
                i18n.get(
                    "msg-s-devices.outdated",
                    title=device_title(device),
                    app=app_title(verdict.app),
                    installed=verdict.installed,
                    latest=verdict.primary.latest,
                )
            )
    return lines


def _stale_lines(
    i18n: TranslatorRunner,
    devices: list[tuple[int, HwidDeviceDto]],
    stale_hours: int,
    icons: dict[int, str],
) -> list[str]:
    lines = []
    for index, device in devices:
        age = subscription_age(device)
        if age < stale_hours * 3600:
            continue
        icons[index] = ICON_STALE
        lines.append(
            i18n.get(
                "msg-s-devices.stale",
                title=device_title(device),
                value=max(1, int(age // 86400)),
            )
        )
    return lines


_OUTCOME_KEYS = {
    SlotOutcome.OPENED: "ntf-s-slots.opened",
    SlotOutcome.ALREADY_OPEN: "ntf-s-slots.already-open",
    SlotOutcome.FREE_PLACE: "ntf-s-slots.free-place",
    SlotOutcome.AT_CEILING: "ntf-s-slots.at-ceiling",
    SlotOutcome.UNAVAILABLE: "ntf-s-slots.unavailable",
}


@inject
async def on_add_slot(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    subscription_dao: FromDishka[SubscriptionDao],
    slots: FromDishka[SlotService],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    subscription = await subscription_dao.get_current(user.id)
    if not subscription:
        return
    result = await slots.open_slot(user, subscription)
    key = _OUTCOME_KEYS.get(result.outcome)
    if not key:
        return
    value = {
        SlotOutcome.OPENED: max(1, result.seconds_left // 60),
        SlotOutcome.ALREADY_OPEN: result.seconds_left,
        SlotOutcome.AT_CEILING: result.ceiling,
    }.get(result.outcome, 0)
    await notifier.notify_user(
        user, payload=MessagePayloadDto(i18n_key=key, i18n_kwargs={"value": value})
    )
