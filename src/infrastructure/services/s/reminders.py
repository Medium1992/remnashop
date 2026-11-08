"""Reminders about one's own devices: an outdated app, a stale subscription.

Both kinds are rate-limited per device: a device that was reminded about is
left alone for ``interval_days``, and the message says so -- a person should
know how often the bot is going to write before it does.
"""

import asyncio
import hashlib
import html
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Optional, Sequence, TypeVar

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from redis.asyncio import Redis
from remnapy.models.hwid import HwidDeviceDto

from src.application.common import Notifier, TranslatorHub
from src.application.dto import MessagePayloadDto, UserDto
from src.application.dto.s_settings import ClientVersionsSettingsDto
from src.core.constants import GOTO_PREFIX
from src.core.utils.client_versions import (
    InstallVerdict,
    Sources,
    VersionsCache,
    app_title,
    check_install,
    notify_worthy,
    parse_client,
)
from src.core.utils.time import datetime_now
from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import DeviceNoticeKey
from src.telegram.states import MainMenu

from .device_scan import DeviceScan, is_router

VERSION_KIND = "version"
STALE_KIND = "stale"
# A device untouched this long is probably abandoned; the reminder says the
# slot can be freed.
ABANDONED_DAYS = 14
SEND_CONCURRENCY = 10


@dataclass(frozen=True)
class OutdatedDevice:
    device: HwidDeviceDto
    verdict: InstallVerdict


@dataclass(frozen=True)
class StaleDevice:
    device: HwidDeviceDto
    age_days: int


@dataclass(frozen=True)
class OutdatedTarget:
    user: UserDto
    items: list[OutdatedDevice]


@dataclass(frozen=True)
class StaleTarget:
    user: UserDto
    items: list[StaleDevice]


T = TypeVar("T", OutdatedTarget, StaleTarget)


@dataclass
class SendReport:
    users: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0


def device_title(device: HwidDeviceDto) -> str:
    title = device.device_model or device.platform or device.hwid[:8]
    return html.escape(str(title))


def subscription_age(device: HwidDeviceDto, now: Optional[datetime] = None) -> float:
    """Seconds since the device last fetched the subscription."""
    moment = device.updated_at
    return ((now or datetime_now()) - moment).total_seconds()


def collect_outdated(
    scan: DeviceScan,
    cache: VersionsCache,
    sources: Sources,
    settings: ClientVersionsSettingsDto,
    router_hwids: list[str],
) -> list[OutdatedTarget]:
    targets = []
    for entry in scan.found:
        items = []
        for device in entry.devices:
            if is_router(device, router_hwids):
                continue
            info = parse_client(device.user_agent, device.platform)
            verdict = check_install(info, cache, sources) if info else None
            if verdict is None or not verdict.outdated:
                continue
            if not notify_worthy(
                verdict.installed,
                verdict.primary.latest,
                ignore_patch=settings.ignore_patch,
                min_minor_gap=settings.min_minor_gap,
            ):
                continue
            items.append(OutdatedDevice(device=device, verdict=verdict))
        if items:
            targets.append(OutdatedTarget(user=entry.user, items=items))
    return targets


def collect_stale(
    scan: DeviceScan,
    stale_hours: int,
    router_hwids: list[str],
    now: Optional[datetime] = None,
) -> list[StaleTarget]:
    """Devices that have not fetched the subscription for ``stale_hours``.

    Routers are left out: they refresh on their own schedule, and the advice
    about a button in Happ / INCY makes no sense there.
    """
    now = now or datetime_now()
    targets = []
    for entry in scan.found:
        items = []
        for device in entry.devices:
            if is_router(device, router_hwids):
                continue
            age = subscription_age(device, now)
            if age >= stale_hours * 3600:
                items.append(StaleDevice(device=device, age_days=int(age // 86400)))
        if items:
            items.sort(key=lambda item: -item.age_days)
            targets.append(StaleTarget(user=entry.user, items=items))
    return targets


def devices_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="btn-s-reminder.devices",
            callback_data=f"{GOTO_PREFIX}{MainMenu.DEVICES.state}",
        )
    )
    return builder.as_markup()


class DeviceReminders:
    def __init__(self, redis: Redis, notifier: Notifier, translator_hub: TranslatorHub) -> None:
        self.redis = redis
        self.notifier = notifier
        self.translator_hub = translator_hub

    @staticmethod
    def _key(kind: str, device: HwidDeviceDto) -> str:
        digest = hashlib.sha1(device.hwid.encode()).hexdigest()[:16]
        return serialize_storage_key(DeviceNoticeKey(kind=kind, device=digest))

    async def _mark(self, kind: str, devices: list[HwidDeviceDto], interval_days: int) -> None:
        ttl = max(1, interval_days) * 86400
        for device in devices:
            await self.redis.set(self._key(kind, device), "1", ex=ttl)

    # ── rendering ───────────────────────────────────────────────────────────

    def render_outdated(self, target: OutdatedTarget, interval_days: int) -> MessagePayloadDto:
        i18n = self.translator_hub.get_translator_by_locale(target.user.language)
        lines = []
        sideload = False
        for item in target.items:
            primary = item.verdict.primary
            sideload = sideload or primary.hint == "sideload"
            lines.append(
                i18n.get(
                    "ntf-s-versions.line",
                    title=device_title(item.device),
                    app=app_title(item.verdict.app),
                    installed=item.verdict.installed,
                    latest=primary.latest,
                )
            )
            if primary.url:
                lines.append(
                    i18n.get(
                        "ntf-s-versions.where",
                        url=html.escape(primary.url, quote=True),
                        label=html.escape(primary.label),
                    )
                )
        return MessagePayloadDto(
            i18n_key="ntf-s-versions.outdated",
            i18n_kwargs={
                "devices": "\n".join(lines),
                "sideload": int(sideload),
                "value": interval_days,
            },
            reply_markup=devices_keyboard(),
            disable_default_markup=False,
            delete_after=None,
        )

    def render_stale(self, target: StaleTarget, interval_days: int) -> MessagePayloadDto:
        i18n = self.translator_hub.get_translator_by_locale(target.user.language)
        lines = []
        apps: list[str] = []
        for item in target.items:
            info = parse_client(item.device.user_agent, item.device.platform)
            app = app_title(info.app) if info else ""
            if app and app not in apps:
                apps.append(app)
            lines.append(
                i18n.get(
                    "ntf-s-stale.line",
                    title=device_title(item.device),
                    app=app or "none",
                    value=max(1, item.age_days),
                )
            )
        oldest = max(item.age_days for item in target.items)
        return MessagePayloadDto(
            i18n_key="ntf-s-stale.reminder",
            i18n_kwargs={
                "devices": "\n".join(lines),
                "apps": " / ".join(apps) or "Happ / INCY",
                "abandoned": int(oldest >= ABANDONED_DAYS),
                "value": interval_days,
            },
            reply_markup=devices_keyboard(),
            disable_default_markup=False,
            delete_after=None,
        )

    # ── sending ─────────────────────────────────────────────────────────────

    async def send_outdated(
        self, targets: list[OutdatedTarget], interval_days: int, *, respect_interval: bool = True
    ) -> SendReport:
        return await self._send(
            VERSION_KIND,
            targets,
            lambda target: self.render_outdated(target, interval_days),
            interval_days,
            respect_interval,
        )

    async def send_stale(
        self, targets: list[StaleTarget], interval_days: int, *, respect_interval: bool = True
    ) -> SendReport:
        return await self._send(
            STALE_KIND,
            targets,
            lambda target: self.render_stale(target, interval_days),
            interval_days,
            respect_interval,
        )

    async def _send(
        self,
        kind: str,
        targets: Sequence[T],
        render: Callable[[T], MessagePayloadDto],
        interval_days: int,
        respect_interval: bool,
    ) -> SendReport:
        report = SendReport(users=len(targets))
        semaphore = asyncio.Semaphore(SEND_CONCURRENCY)

        async def one(target: T) -> None:
            if respect_interval:
                # Only the devices not reminded about recently are listed.
                due = [
                    i
                    for i in target.items
                    if not await self.redis.exists(self._key(kind, i.device))
                ]
                if not due:
                    report.skipped += 1
                    return
                target = replace(target, items=due)
            async with semaphore:
                try:
                    await self.notifier.notify_user(target.user, payload=render(target))
                except Exception as e:
                    logger.warning(f"Device reminder '{kind}' to {target.user.log} failed: {e}")
                    report.failed += 1
                    return
            await self._mark(kind, [i.device for i in target.items], interval_days)
            report.sent += 1

        await asyncio.gather(*(one(target) for target in targets))
        logger.info(
            f"Device reminders '{kind}': '{report.sent}' sent, '{report.skipped}' skipped, "
            f"'{report.failed}' failed of '{report.users}'"
        )
        return report
