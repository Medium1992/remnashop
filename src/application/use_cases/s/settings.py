"""Changing the "s" edition's settings (``settings.extra.s``)."""

from dataclasses import dataclass
from typing import Any, Final, Optional

from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import SettingsDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import UserDto
from src.core.utils.client_versions import DEFAULT_SOURCES, EDITABLE_KEYS

# Every number an admin may type in, with the range it must fall in.
NUMBER_FIELDS: Final[dict[str, tuple[int, int]]] = {
    "slots.base_limit": (0, 50),
    "slots.slot_ttl_minutes": (1, 60),
    "versions.refresh_hours": (1, 168),
    "versions.interval_days": (1, 90),
    "versions.observed_min_devices": (1, 1000),
    "stale.stale_hours": (12, 24 * 60),
    "stale.interval_days": (1, 90),
    "inactive.delete_after_days": (1, 3650),
    "inactive.warn_days": (0, 60),
    "chat_access.grace_hours": (0, 24 * 30),
}

TOGGLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "slots.enabled",
        "versions.enabled",
        "versions.notify_enabled",
        "versions.ignore_patch",
        "stale.enabled",
        "inactive.enabled",
        "inactive.include_disabled",
        "chat_access.enabled",
    }
)


def _split(path: str) -> tuple[str, str]:
    section, _, name = path.partition(".")
    return section, name


@dataclass(frozen=True)
class SetSSettingDto:
    path: str  # "<section>.<field>", one of NUMBER_FIELDS
    raw_value: str


class SetSSetting(Interactor[SetSSettingDto, int]):
    """Set a numeric setting. Raises ValueError for anything out of range."""

    required_permission = Permission.SETTINGS_EXTRA

    def __init__(self, uow: UnitOfWork, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, data: SetSSettingDto) -> int:
        bounds = NUMBER_FIELDS.get(data.path)
        if bounds is None:
            raise ValueError(f"Unknown setting '{data.path}'")
        value = int(data.raw_value.strip())
        low, high = bounds
        if not low <= value <= high:
            raise ValueError(f"'{data.path}' must be within {low}..{high}")

        section, name = _split(data.path)
        async with self.uow:
            settings = await self.settings_dao.get()
            setattr(getattr(settings.extra.s, section), name, value)
            await self.settings_dao.update(settings)
            await self.uow.commit()

        logger.info(f"{actor.log} Set s.{data.path} = {value}")
        return value


class ToggleSSetting(Interactor[str, bool]):
    required_permission = Permission.SETTINGS_EXTRA

    def __init__(self, uow: UnitOfWork, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, path: str) -> bool:
        if path not in TOGGLE_FIELDS:
            raise ValueError(f"Unknown toggle '{path}'")
        section, name = _split(path)
        async with self.uow:
            settings = await self.settings_dao.get()
            target = getattr(settings.extra.s, section)
            value = not getattr(target, name)
            setattr(target, name, value)
            await self.settings_dao.update(settings)
            await self.uow.commit()

        logger.info(f"{actor.log} Toggled s.{path} = {value}")
        return bool(value)


@dataclass(frozen=True)
class UpdateVersionChannelDto:
    app: str
    channel: str
    # None resets the channel to its built-in source.
    patch: Optional[dict[str, Any]]


class UpdateVersionChannel(Interactor[UpdateVersionChannelDto, None]):
    required_permission = Permission.SETTINGS_EXTRA

    def __init__(self, uow: UnitOfWork, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, data: UpdateVersionChannelDto) -> None:
        if data.channel not in DEFAULT_SOURCES.get(data.app, {}).get("channels", {}):
            raise ValueError(f"Unknown channel '{data.app}/{data.channel}'")
        if data.patch and not set(data.patch) <= EDITABLE_KEYS:
            raise ValueError(f"Not editable: {set(data.patch) - EDITABLE_KEYS}")

        async with self.uow:
            settings = await self.settings_dao.get()
            versions = settings.extra.s.versions
            # A fresh dict, so the change is seen and the whole block written.
            sources = {
                app: {k: dict(v) for k, v in ch.items()} for app, ch in versions.sources.items()
            }
            channels = sources.setdefault(data.app, {})
            if data.patch is None:
                channels.pop(data.channel, None)
            else:
                channels.setdefault(data.channel, {}).update(data.patch)
            versions.sources = sources
            await self.settings_dao.update(settings)
            await self.uow.commit()

        logger.info(
            f"{actor.log} Version channel '{data.app}/{data.channel}' "
            + ("reset" if data.patch is None else f"updated: {sorted(data.patch)}")
        )


class UpdateRouterHwids(Interactor[str, list[str]]):
    """Replace the router HWID list from free text: one per line or comma."""

    required_permission = Permission.SETTINGS_EXTRA

    def __init__(self, uow: UnitOfWork, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, raw: str) -> list[str]:
        hwids: list[str] = []
        if raw.strip() != "-":
            for chunk in raw.replace(",", "\n").splitlines():
                value = chunk.strip()
                if value and value not in hwids:
                    hwids.append(value)

        async with self.uow:
            settings = await self.settings_dao.get()
            settings.extra.s.router_hwids = hwids
            await self.settings_dao.update(settings)
            await self.uow.commit()

        logger.info(f"{actor.log} Router HWIDs set: {len(hwids)}")
        return hwids
