from dishka import Provider, Scope, provide

from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.infrastructure.services.s.device_scan import DeviceScanner
from src.infrastructure.services.s.jobs import DeviceJobs
from src.infrastructure.services.s.reminders import DeviceReminders


class SProvider(Provider):
    """Services of the "s" edition (docs/s/README.md)."""

    scope = Scope.REQUEST

    client_versions = provide(source=ClientVersionsService, scope=Scope.APP)
    device_scanner = provide(source=DeviceScanner)
    device_reminders = provide(source=DeviceReminders)
    device_jobs = provide(source=DeviceJobs)
