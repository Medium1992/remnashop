from dishka import AnyOf, Provider, Scope, provide

from src.application.common import Remnawave
from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.infrastructure.services.s.device_scan import DeviceScanner
from src.infrastructure.services.s.jobs import DeviceJobs
from src.infrastructure.services.s.reminders import DeviceReminders
from src.infrastructure.services.s.remnawave import SRemnawave
from src.infrastructure.services.s.slots import SlotService


class SProvider(Provider):
    """Services of the "s" edition (docs/s/README.md)."""

    scope = Scope.REQUEST

    # Replaces the upstream panel client: the same class plus strict slots.
    remnawave = provide(
        source=SRemnawave,
        provides=AnyOf[Remnawave, SRemnawave],
        scope=Scope.APP,
        override=True,
    )

    client_versions = provide(source=ClientVersionsService, scope=Scope.APP)
    device_scanner = provide(source=DeviceScanner)
    device_reminders = provide(source=DeviceReminders)
    device_jobs = provide(source=DeviceJobs)
    slots = provide(source=SlotService)
