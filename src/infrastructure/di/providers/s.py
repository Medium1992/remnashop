from dishka import AnyOf, Provider, Scope, provide, provide_all

from src.application.common import Remnawave
from src.application.services.s_plan_change import PlanChange
from src.application.use_cases.s import S_USE_CASES
from src.infrastructure.database.dao.s_blocks import UserBlockDao
from src.infrastructure.services.s.blocks import BlockService
from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.infrastructure.services.s.device_scan import DeviceScanner
from src.infrastructure.services.s.inactive import InactiveProfiles
from src.infrastructure.services.s.invites import InviteLinks
from src.infrastructure.services.s.jobs import DeviceJobs
from src.infrastructure.services.s.reminders import DeviceReminders
from src.infrastructure.services.s.remnawave import SRemnawave
from src.infrastructure.services.s.slots import SlotService
from src.infrastructure.services.s.texts import TextOverrides


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
    inactive = provide(source=InactiveProfiles)
    block_dao = provide(source=UserBlockDao)
    blocks = provide(source=BlockService)
    invites = provide(source=InviteLinks)
    plan_change = provide(source=PlanChange)
    texts = provide(source=TextOverrides, scope=Scope.APP)

    use_cases = provide_all(*S_USE_CASES)
