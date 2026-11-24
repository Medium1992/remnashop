"""The "s" additions to the user's own devices screen.

Hooked into the upstream screen at two points only: its getter merges
``devices_extra`` into what it returns, and its window shows ``s_devices`` and
the add-slot button.
"""

import html
from typing import Any, Optional

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
from src.application.dto.s_blocks import UserBlockDto
from src.application.dto.s_settings import SSettingsDto
from src.core.constants import USER_KEY
from src.core.utils.client_versions import (
    ClientInfo,
    InstallVerdict,
    Sources,
    VersionsCache,
    app_title,
    check_install,
    compare,
    merge_sources,
    parse_client,
)
from src.core.utils.s_time import DATE_TIME, tg_time
from src.infrastructure.services.s.blocks import BlockService, local_until
from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.infrastructure.services.s.device_scan import is_router
from src.infrastructure.services.s.reminders import subscription_age
from src.infrastructure.services.s.slot_grants import read_grant, shown_counters
from src.infrastructure.services.s.slots import SlotOutcome, SlotService

CARDS_KEY = "s_device_cards"

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

    blocks: BlockService = await container.get(BlockService)
    block = await blocks.get(subscription.user_id)
    if block:
        lines.append(block_banner(i18n, block))

    if s.slots.enabled and subscription.device_limit > 0 and not block:
        occupied, shown = shown_counters(subscription.device_limit, len(devices))
        extra["max_count"] = shown
        extra["s_slot_button"] = int(occupied < subscription.device_limit)
        grant = await read_grant(await container.get(Redis), subscription.user_remna_id)
        if grant and grant.is_open:
            lines.append(i18n.get("msg-s-devices.slot-open", seconds=grant.seconds_left))
        elif extra["s_slot_button"]:
            lines.append(i18n.get("msg-s-devices.slot-hint", value=s.slots.slot_ttl_minutes))

    lines += await _device_lines(dialog_manager, container, i18n, s, devices, icons)

    for index, icon in icons.items():
        if index < len(formatted):
            formatted[index]["label"] = f"{icon} {formatted[index]['label']}"

    extra["s_devices"] = "\n".join(lines)
    return extra


async def _device_lines(
    dialog_manager: DialogManager,
    container: AsyncContainer,
    i18n: TranslatorRunner,
    s: SSettingsDto,
    devices: list[HwidDeviceDto],
    icons: dict[int, str],
) -> list[str]:
    """Short summaries for the list; each device's details are kept for its card."""
    lines: list[str] = []
    own = [(i, d) for i, d in enumerate(devices) if not is_router(d, s.router_hwids)]
    cards: dict[str, list[str]] = {d.hwid: [] for d in devices}
    for device in devices:
        if is_router(device, s.router_hwids):
            cards[device.hwid].append(i18n.get("msg-s-device-card.router"))
    if s.versions.enabled:
        versions: ClientVersionsService = await container.get(ClientVersionsService)
        sources = merge_sources(s.versions.sources)
        lines += _version_lines(i18n, own, await versions.load(), sources, s, icons, cards)
    if s.stale.enabled:
        lines += _stale_lines(i18n, own, s.stale.stale_hours, icons, cards)
    # what the device's card shows when it is tapped (details and what to do)
    dialog_manager.dialog_data[CARDS_KEY] = {
        hwid: "\n\n".join(parts) for hwid, parts in cards.items() if parts
    }
    return lines


def block_banner(i18n: TranslatorRunner, block: UserBlockDto) -> str:
    return i18n.get(
        "msg-s-block.banner",
        until=local_until(block.until) if block.until else "none",
        reason=block.reason or "none",
    )


def _version_lines(
    i18n: TranslatorRunner,
    devices: list[tuple[int, HwidDeviceDto]],
    cache: VersionsCache,
    sources: Sources,
    s: SSettingsDto,
    icons: dict[int, str],
    cards: dict[str, list[str]],
) -> list[str]:
    """One summary line for the list; the details go to each device's card."""
    outdated = known = 0
    for index, device in devices:
        info = parse_client(device.user_agent, device.platform)
        verdict = check_install(info, cache, sources) if info else None
        if verdict is None:
            cards[device.hwid].append(_unknown_card(i18n, info))
            continue
        known += 1
        worthy = verdict.needs_update(
            ignore_patch=s.versions.ignore_patch, min_minor_gap=s.versions.min_minor_gap
        )
        icons[index] = ICON_OUTDATED if worthy else ICON_FRESH
        outdated += int(worthy)
        cards[device.hwid].append(_version_card(i18n, verdict, worthy))
    if outdated:
        return [i18n.get("msg-s-devices.summary-outdated", count=outdated, total=len(devices))]
    return [i18n.get("msg-s-devices.summary-current")] if known else []


def _unknown_card(i18n: TranslatorRunner, info: Optional[ClientInfo]) -> str:
    if info is None:
        return i18n.get("msg-s-device-card.unknown-app")
    return i18n.get(
        "msg-s-device-card.unknown-version",
        app=html.escape(app_title(info.app)),
        installed=html.escape(info.version),
    )


def _version_card(i18n: TranslatorRunner, verdict: InstallVerdict, worthy: bool) -> str:
    primary = verdict.primary
    app = html.escape(app_title(verdict.app))
    installed = html.escape(verdict.installed)
    if not worthy:
        store = verdict.store_latest
        if store is not None:
            # the newest from the store it most likely came from: nothing to fix
            return i18n.get(
                "msg-s-device-card.store-latest",
                app=app,
                installed=installed,
                store=html.escape(store.label),
                latest=primary.latest,
                elsewhere=html.escape(primary.label),
                url=html.escape(primary.url, quote=True) if primary.url else "none",
            )
        if verdict.outdated:
            # a gap too small to bother anyone with: said honestly, not "latest"
            return i18n.get(
                "msg-s-device-card.minor", app=app, installed=installed, latest=primary.latest
            )
        return i18n.get(
            "msg-s-device-card.current",
            app=app,
            installed=installed,
            label=html.escape(primary.label),
        )
    parts = [
        i18n.get("msg-s-device-card.outdated", app=app, installed=installed, latest=primary.latest)
    ]
    # "but I have the latest from the store" — it is, for that store: say so
    same = next(
        (
            alt
            for alt in verdict.alternatives
            if alt.latest and compare(alt.latest, verdict.installed) <= 0
        ),
        None,
    )
    if same is not None:
        parts.append(i18n.get("msg-s-device-card.same-as-store", label=html.escape(same.label)))
    if primary.url:
        parts.append(
            i18n.get(
                "msg-s-device-card.download",
                url=html.escape(primary.url, quote=True),
                label=html.escape(primary.label),
            )
        )
    if primary.hint == "sideload":
        parts.append(i18n.get("msg-s-device-card.sideload"))
    elif verdict.platform in ("ios", "macos"):
        parts.append(i18n.get("msg-s-device-card.how-ios"))
    elif verdict.platform == "android":
        parts.append(i18n.get("msg-s-device-card.how-android"))
    elif verdict.platform in ("windows", "linux"):
        parts.append(i18n.get("msg-s-device-card.how-desktop"))
    return "\n\n".join(parts)


def _age(i18n: TranslatorRunner, seconds: float) -> str:
    days, hours = int(seconds // 86400), int(seconds // 3600)
    if days:
        return i18n.get("msg-s-device-card.age-days", value=days)
    if hours:
        return i18n.get("msg-s-device-card.age-hours", value=hours)
    return i18n.get("msg-s-device-card.age-minutes")


def _stale_lines(
    i18n: TranslatorRunner,
    devices: list[tuple[int, HwidDeviceDto]],
    stale_hours: int,
    icons: dict[int, str],
    cards: dict[str, list[str]],
) -> list[str]:
    stale: list[HwidDeviceDto] = []
    for index, device in devices:
        age = subscription_age(device)
        when = tg_time(device.updated_at, DATE_TIME, device.updated_at.strftime("%d.%m.%Y %H:%M"))
        info = parse_client(device.user_agent, device.platform)
        app = html.escape(app_title(info.app)) if info else ""
        if age < stale_hours * 3600:
            cards[device.hwid].append(
                i18n.get("msg-s-device-card.sub-fresh", age=_age(i18n, age), when=when)
            )
            continue
        stale.append(device)
        # both marks when the app is outdated as well
        both = icons.get(index) == ICON_OUTDATED
        icons[index] = ICON_OUTDATED + ICON_STALE if both else ICON_STALE
        cards[device.hwid].append(
            i18n.get(
                "msg-s-device-card.sub-stale",
                age=_age(i18n, age),
                when=when,
                app=app or "none",
            )
        )
    if not stale:
        return []
    apps: list[str] = []
    for device in stale:
        info = parse_client(device.user_agent, device.platform)
        title = app_title(info.app) if info else ""
        if title and title not in apps:
            apps.append(title)
    return [
        i18n.get(
            "msg-s-devices.summary-stale",
            count=len(stale),
            total=len(devices),
            hours=stale_hours,
            apps=html.escape(" / ".join(apps) or "Happ / INCY"),
        )
    ]


async def device_card_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    """The device's details for its card (the upstream delete window)."""
    hwid = dialog_manager.dialog_data.get("selected_hwid")
    cards = dialog_manager.dialog_data.get(CARDS_KEY) or {}
    return {"s_device_card": cards.get(hwid, "") if hwid else ""}


_OUTCOME_KEYS = {
    SlotOutcome.OPENED: "ntf-s-slots.opened",
    SlotOutcome.ALREADY_OPEN: "ntf-s-slots.already-open",
    SlotOutcome.FREE_PLACE: "ntf-s-slots.free-place",
    SlotOutcome.AT_CEILING: "ntf-s-slots.at-ceiling",
    SlotOutcome.UNAVAILABLE: "ntf-s-slots.unavailable",
    SlotOutcome.BLOCKED: "ntf-s-slots.blocked",
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
