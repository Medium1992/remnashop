import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.application.common.dao import SettingsDao
from src.core.utils.client_versions import VersionsCache, merge_sources

from .client_versions import ClientVersionsService
from .device_scan import DeviceScan, DeviceScanner
from .reminders import (
    DeviceReminders,
    SendReport,
    collect_outdated,
    collect_stale,
)


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
    ) -> None:
        self.settings_dao = settings_dao
        self.scanner = scanner
        self.versions = versions
        self.reminders = reminders

    async def refresh_versions(self, *, force: bool = False) -> Optional[VersionsCache]:
        settings = (await self.settings_dao.get()).extra.s.versions
        if not settings.enabled and not force:
            return None

        cache = await self.versions.load()
        # Reading every device only pays off when the observed fallback is due.
        observed_due = force or time.time() - cache.observed_at >= settings.refresh_hours * 3600
        clients = (await self.scanner.scan()).clients() if observed_due else None
        return await self.versions.refresh(settings, clients=clients, force=force)

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
