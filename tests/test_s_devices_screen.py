"""What the user's devices screen gains in the "s" edition."""

from typing import Any, Dict, Optional

import pytest
from dishka import make_container
from dishka.integrations.aiogram import CONTAINER_NAME
from redis.asyncio import Redis
from test_s_reminders import a_device
from test_service_end_to_end import a_subscription

from src.application.common import TranslatorHub
from src.application.dto import SettingsDto
from src.application.dto.s_blocks import UserBlockDto
from src.core.config import AppConfig
from src.core.utils.client_versions import ChannelState, VersionsCache
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.di.providers.retort import RetortProvider
from src.infrastructure.services.s.blocks import BlockService
from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.telegram.routers.s.devices import devices_extra


@pytest.fixture(scope="module")
def i18n() -> Any:
    container = make_container(
        RetortProvider(), I18nProvider(), context={AppConfig: AppConfig.get()}, skip_validation=True
    )
    return container.get(TranslatorHub).get_translator_by_locale("ru")


class FakeRedis:
    async def get(self, name: str) -> Optional[str]:
        return None


class FakeVersions:
    async def load(self) -> VersionsCache:
        cache = VersionsCache()
        cache.channels["happ/android"] = ChannelState(version="4.3.0", status="ok")
        return cache


class FakeBlocks:
    def __init__(self, block: Optional[UserBlockDto] = None) -> None:
        self.block = block

    async def get(self, user_id: int) -> Optional[UserBlockDto]:
        return self.block


class FakeContainer:
    def __init__(self, block: Optional[UserBlockDto] = None) -> None:
        self.blocks = FakeBlocks(block)

    async def get(self, kind: Any) -> Any:
        return {
            Redis: FakeRedis(),
            ClientVersionsService: FakeVersions(),
            BlockService: self.blocks,
        }[kind]


class FakeManager:
    def __init__(self, block: Optional[UserBlockDto] = None) -> None:
        self.middleware_data: Dict[str, Any] = {CONTAINER_NAME: FakeContainer(block)}


def settings(**on: bool) -> SettingsDto:
    value = SettingsDto()
    value.extra.s.slots.enabled = on.get("slots", False)
    value.extra.s.versions.enabled = on.get("versions", False)
    value.extra.s.stale.enabled = on.get("stale", False)
    return value


def labels(n: int) -> list[dict[str, Any]]:
    return [{"label": f"device {i}"} for i in range(n)]


class TestDevicesScreen:
    async def test_everything_off_adds_nothing(self, i18n: Any) -> None:
        formatted = labels(1)
        extra = await devices_extra(
            FakeManager(),  # type: ignore[arg-type]
            i18n,
            settings(),
            a_subscription(device_limit=3),
            [a_device("a", "Happ/4.1.0/Android/1")],
            formatted,
        )
        assert extra == {"s_slot_button": 0, "s_devices": ""}
        assert formatted[0]["label"] == "device 0"

    async def test_slots_show_the_plan_ceiling_and_the_button(self, i18n: Any) -> None:
        extra = await devices_extra(
            FakeManager(),  # type: ignore[arg-type]
            i18n,
            settings(slots=True),
            a_subscription(device_limit=3),
            [a_device("a", "Happ/4.3.0/Android/1")],
            labels(1),
        )
        assert extra["max_count"] == 3
        assert extra["s_slot_button"] == 1
        assert "Добавить устройство" in extra["s_devices"]

    async def test_no_button_at_the_ceiling(self, i18n: Any) -> None:
        devices = [a_device(str(i), "Happ/4.3.0/Android/1") for i in range(3)]
        extra = await devices_extra(
            FakeManager(),  # type: ignore[arg-type]
            i18n,
            settings(slots=True),
            a_subscription(device_limit=2),
            devices,
            labels(3),
        )
        assert extra["s_slot_button"] == 0
        assert extra["max_count"] == 3  # never "3 of 2"

    async def test_versions_and_stale_mark_the_buttons(self, i18n: Any) -> None:
        devices = [
            a_device("old-app", "Happ/4.1.0/Android/1"),
            a_device("fresh", "Happ/4.3.0/Android/1"),
            a_device("stale", "Happ/4.3.0/Android/1", days_ago=10),
        ]
        formatted = labels(3)
        extra = await devices_extra(
            FakeManager(),  # type: ignore[arg-type]
            i18n,
            settings(versions=True, stale=True),
            a_subscription(device_limit=5),
            devices,
            formatted,
        )
        assert [f["label"][:1] for f in formatted] == ["⚠", "✅", "⏳"]
        assert "4.1.0 → <b>4.3.0</b>" in extra["s_devices"]
        assert "не обновлялась 10 дней" in extra["s_devices"]

    async def test_a_block_is_shown_and_hides_the_slot_button(self, i18n: Any) -> None:
        extra = await devices_extra(
            FakeManager(UserBlockDto(user_id=7, reason="торренты")),  # type: ignore[arg-type]
            i18n,
            settings(slots=True),
            a_subscription(device_limit=3),
            [],
            [],
        )
        assert "Доступ приостановлен" in extra["s_devices"]
        assert "торренты" in extra["s_devices"]
        assert extra["s_slot_button"] == 0
