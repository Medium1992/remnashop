import asyncio
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from remnapy.models.hwid import HwidDeviceDto

from src.application.common import Remnawave
from src.application.common.dao import SubscriptionDao, UserDao
from src.application.dto import SubscriptionDto, UserDto
from src.core.utils.client_versions import ROUTER_PLATFORM, ClientInfo, parse_client

PANEL_CONCURRENCY = 5


@dataclass(frozen=True)
class UserDevices:
    user: UserDto
    subscription: SubscriptionDto
    devices: list[HwidDeviceDto]


@dataclass(frozen=True)
class DeviceScan:
    found: list[UserDevices]
    # Users whose device list the panel did not return. They are unknown, not
    # device-less: nothing may be concluded about them from this scan.
    failed: int

    def clients(self) -> list[ClientInfo]:
        infos = []
        for entry in self.found:
            for device in entry.devices:
                info = parse_client(device.user_agent, device.platform)
                if info:
                    infos.append(info)
        return infos


def is_router(device: HwidDeviceDto, router_hwids: list[str]) -> bool:
    if device.hwid in router_hwids:
        return True
    info = parse_client(device.user_agent, device.platform)
    if info and info.platform == ROUTER_PLATFORM:
        return True
    return (device.platform or "").lower().startswith("routeros")


class DeviceScanner:
    """Every active subscriber's HWID devices, read from the panel.

    Goes user by user through the call both panel lines share, so it works the
    same on 2.x and 3.x. The database is read first and sequentially -- one
    session cannot run queries concurrently -- and only the panel calls run in
    parallel.
    """

    def __init__(
        self,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        remnawave: Remnawave,
    ) -> None:
        self.user_dao = user_dao
        self.subscription_dao = subscription_dao
        self.remnawave = remnawave

    async def _subscribers(self) -> list[tuple[UserDto, SubscriptionDto]]:
        pairs = []
        for user in await self.user_dao.get_with_active_subscription():
            subscription = await self.subscription_dao.get_current(user.id)
            if subscription and subscription.is_active:
                pairs.append((user, subscription))
        return pairs

    async def scan(self) -> DeviceScan:
        pairs = await self._subscribers()
        semaphore = asyncio.Semaphore(PANEL_CONCURRENCY)

        async def read(user: UserDto, sub: SubscriptionDto) -> Optional[UserDevices]:
            async with semaphore:
                try:
                    devices = await self.remnawave.get_devices(sub.user_remna_id)
                except Exception as e:
                    logger.warning(f"Device list for {user.log} unavailable: {e}")
                    return None
            return UserDevices(user=user, subscription=sub, devices=devices)

        results = await asyncio.gather(*(read(user, sub) for user, sub in pairs))
        found = [r for r in results if r is not None]
        scan = DeviceScan(found=found, failed=len(results) - len(found))
        logger.info(
            f"Device scan: '{len(found)}' subscribers read, '{scan.failed}' unavailable, "
            f"'{sum(len(e.devices) for e in found)}' devices"
        )
        return scan
