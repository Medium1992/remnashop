from dishka import AnyOf, Provider, Scope, provide, provide_all
from redis.asyncio import Redis

from src.application.common import Remnawave
from src.application.services.s_kept import KeptLimits
from src.application.services.s_plan_change import PlanChange
from src.application.use_cases.referral.commands.rewards import GiveReferrerReward
from src.application.use_cases.s import S_USE_CASES
from src.core.config import AppConfig
from src.core.utils.s_cabinet import CabinetConfig, signing_key
from src.core.utils.s_cabinet import cabinet_config as s_cabinet_config
from src.infrastructure.database.dao.s_blocks import UserBlockDao
from src.infrastructure.database.dao.s_bonus import BonusDaysDao
from src.infrastructure.database.dao.s_kept import KeptLimitsDao
from src.infrastructure.database.dao.s_receipts import NalogAccountDao, ReceiptDao
from src.infrastructure.services.s.blocks import BlockService
from src.infrastructure.services.s.bonus import BonusDays, SGiveReferrerReward
from src.infrastructure.services.s.cabinet_sessions import CabinetSessions
from src.infrastructure.services.s.channels import GatewayChannels
from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.infrastructure.services.s.device_scan import DeviceScanner
from src.infrastructure.services.s.inactive import InactiveProfiles
from src.infrastructure.services.s.invites import InviteLinks
from src.infrastructure.services.s.jobs import DeviceJobs
from src.infrastructure.services.s.nalog_auth import NalogAuth
from src.infrastructure.services.s.receipts import Receipts
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
    bonus_dao = provide(source=BonusDaysDao)
    bonus = provide(source=BonusDays)
    kept_dao = provide(source=KeptLimitsDao)
    kept = provide(source=KeptLimits)
    nalog_account_dao = provide(source=NalogAccountDao)
    nalog_auth = provide(source=NalogAuth)
    receipt_dao = provide(source=ReceiptDao)
    receipts = provide(source=Receipts)
    gateway_channels = provide(source=GatewayChannels)

    @provide(scope=Scope.APP)
    def cabinet_config(self) -> CabinetConfig:
        # resolved only by the cabinet's routes, which exist only when it is set
        config = s_cabinet_config()
        if config is None:
            raise RuntimeError("The web cabinet is not configured")
        return config

    @provide(scope=Scope.APP)
    def cabinet_sessions(self, redis: Redis, config: AppConfig) -> CabinetSessions:
        return CabinetSessions(redis, signing_key(config.crypt_key.get_secret_value()))

    # Keeps referral days while bonus days are on; upstream's reward otherwise.
    give_referrer_reward = provide(
        source=SGiveReferrerReward,
        provides=AnyOf[GiveReferrerReward, SGiveReferrerReward],
        override=True,
    )

    use_cases = provide_all(*S_USE_CASES)
