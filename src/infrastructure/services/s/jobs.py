import time
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Optional, Union

from loguru import logger

from src.application.common import Notifier, TranslatorHub
from src.application.common.dao import SettingsDao, UserDao
from src.application.dto import MessagePayloadDto, UserDto
from src.application.dto.s_settings import SSettingsDto
from src.core.utils.client_versions import VersionsCache, app_title, merge_sources

from .client_versions import ClientVersionsService
from .device_scan import DeviceScan, DeviceScanner
from .reminders import (
    DeviceReminders,
    OutdatedDevice,
    OutdatedTarget,
    SendReport,
    StaleTarget,
    collect_outdated,
    collect_stale,
    device_title,
)

# Telegram allows 4096 characters; leave room for the markup.
CHUNK_LIMIT = 3500


class ReminderKind(StrEnum):
    VERSIONS = auto()
    STALE = auto()


class AdminAction(StrEnum):
    SUMMARY = auto()  # who is behind, as text for the admin
    PREVIEW = auto()  # the message one user would get, sent to the admin
    SEND = auto()  # send to everyone now, ignoring the interval


@dataclass
class RemindersReport:
    versions: Optional[SendReport] = None
    stale: Optional[SendReport] = None
    unavailable: int = 0


class DeviceJobs:
    """The scheduled work behind versions and reminders.

    The panel is read once per run and the result shared: the device scan is
    the expensive part, one call per subscriber.
    """

    def __init__(
        self,
        settings_dao: SettingsDao,
        scanner: DeviceScanner,
        versions: ClientVersionsService,
        reminders: DeviceReminders,
        user_dao: UserDao,
        notifier: Notifier,
        translator_hub: TranslatorHub,
    ) -> None:
        self.settings_dao = settings_dao
        self.scanner = scanner
        self.versions = versions
        self.reminders = reminders
        self.user_dao = user_dao
        self.notifier = notifier
        self.translator_hub = translator_hub

    async def refresh_versions(self, *, force: bool = False) -> Optional[VersionsCache]:
        settings = (await self.settings_dao.get()).extra.s.versions
        if not settings.enabled and not force:
            return None

        cache = await self.versions.load()
        # Reading every device only pays off when the observed fallback is due.
        observed_due = force or time.time() - cache.observed_at >= settings.refresh_hours * 3600
        clients = (await self.scanner.scan()).clients() if observed_due else None
        return await self.versions.refresh(settings, clients=clients, force=force)

    async def refresh_channel(self, app: str, channel: str) -> None:
        settings = (await self.settings_dao.get()).extra.s.versions
        await self.versions.refresh(settings, force=True, only=(app, channel))

    async def send_reminders(self, scan: Optional[DeviceScan] = None) -> RemindersReport:
        s = (await self.settings_dao.get()).extra.s
        report = RemindersReport()
        want_versions = s.versions.enabled and s.versions.notify_enabled
        if not want_versions and not s.stale.enabled:
            return report

        scan = scan or await self.scanner.scan()
        report.unavailable = scan.failed

        if want_versions:
            cache = await self.versions.load()
            targets = collect_outdated(
                scan, cache, merge_sources(s.versions.sources), s.versions, s.router_hwids
            )
            report.versions = await self.reminders.send_outdated(targets, s.versions.interval_days)

        if s.stale.enabled:
            targets_stale = collect_stale(scan, s.stale.stale_hours, s.router_hwids)
            report.stale = await self.reminders.send_stale(targets_stale, s.stale.interval_days)

        logger.info(f"Device reminders done: {report}")
        return report

    # ── admin actions ───────────────────────────────────────────────────────

    async def _targets(
        self, kind: ReminderKind, s: SSettingsDto, scan: DeviceScan
    ) -> Union[list[OutdatedTarget], list[StaleTarget]]:
        if kind == ReminderKind.VERSIONS:
            cache = await self.versions.load()
            sources = merge_sources(s.versions.sources)
            return collect_outdated(scan, cache, sources, s.versions, s.router_hwids)
        return collect_stale(scan, s.stale.stale_hours, s.router_hwids)

    async def _tell(self, admin: UserDto, key: str, **kwargs: object) -> None:
        await self.notifier.notify_user(
            admin,
            payload=MessagePayloadDto(i18n_key=key, i18n_kwargs=dict(kwargs), delete_after=None),
        )

    async def admin_action(
        self,
        admin_telegram_id: int,
        kind: ReminderKind,
        action: AdminAction,
        target_telegram_id: Optional[int] = None,
    ) -> None:
        admin = await self.user_dao.get_by_telegram_id(admin_telegram_id)
        if not admin:
            return
        s = (await self.settings_dao.get()).extra.s
        scan = await self.scanner.scan(only_telegram_id=target_telegram_id)
        targets = await self._targets(kind, s, scan)
        interval = (
            s.versions.interval_days if kind == ReminderKind.VERSIONS else s.stale.interval_days
        )

        if action == AdminAction.SUMMARY:
            for chunk in self.summary(admin, kind, targets, scan.failed):
                await self._tell(admin, "ntf-s-admin.text", content=chunk)
            return

        if action == AdminAction.PREVIEW:
            if not targets:
                await self._tell(admin, "ntf-s-admin.nothing", target=target_telegram_id or 0)
                return
            target = targets[0]
            if isinstance(target, OutdatedTarget):
                payload = self.reminders.render_outdated(target, interval)
            else:
                payload = self.reminders.render_stale(target, interval)
            await self.notifier.notify_user(admin, payload=payload)
            return

        if kind == ReminderKind.VERSIONS:
            report = await self.reminders.send_outdated(
                targets,  # type: ignore[arg-type]
                interval,
                respect_interval=False,
            )
        else:
            report = await self.reminders.send_stale(
                targets,  # type: ignore[arg-type]
                interval,
                respect_interval=False,
            )
        await self._tell(
            admin,
            "ntf-s-admin.sent",
            sent=report.sent,
            failed=report.failed,
            users=report.users,
        )

    def summary(
        self,
        admin: UserDto,
        kind: ReminderKind,
        targets: Union[list[OutdatedTarget], list[StaleTarget]],
        unavailable: int,
    ) -> list[str]:
        """Who is behind, worst first, split into messages Telegram accepts."""
        i18n = self.translator_hub.get_translator_by_locale(admin.language)
        devices = sum(len(t.items) for t in targets)
        header = i18n.get(
            "ntf-s-admin.summary-header",
            kind=str(kind),
            users=len(targets),
            devices=devices,
            unavailable=unavailable,
        )
        blocks = []
        for target in targets:
            lines = [
                i18n.get(
                    "ntf-s-admin.summary-user",
                    name=target.user.name,
                    telegram_id=target.user.telegram_id or 0,
                )
            ]
            for item in target.items:
                if isinstance(item, OutdatedDevice):
                    lines.append(
                        i18n.get(
                            "ntf-s-admin.summary-outdated",
                            title=device_title(item.device),
                            app=app_title(item.verdict.app),
                            installed=item.verdict.installed,
                            latest=item.verdict.primary.latest,
                        )
                    )
                else:
                    lines.append(
                        i18n.get(
                            "ntf-s-admin.summary-stale",
                            title=device_title(item.device),
                            value=max(1, item.age_days),
                        )
                    )
            blocks.append("\n".join(lines))

        chunks: list[str] = []
        current = header
        for block in blocks:
            if len(current) + len(block) + 2 > CHUNK_LIMIT:
                chunks.append(current)
                current = ""
            current += ("\n\n" if current else "") + block
        chunks.append(current)
        return chunks
