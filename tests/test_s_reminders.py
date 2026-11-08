"""Reminders about an outdated app and a stale subscription.

Rendered through the real translations, so a broken .ftl fails here rather
than in a user's chat. Each device is reminded about at most once per
interval, and the message tells the person that interval.
"""

from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest
from adaptix import Retort
from dishka import make_container
from remnapy.models.hwid import HwidDeviceDto
from test_service_end_to_end import a_subscription

from src.application.common import TranslatorHub
from src.application.dto import UserDto
from src.application.dto.s_settings import ClientVersionsSettingsDto
from src.core.config import AppConfig
from src.core.utils.client_versions import ChannelState, VersionsCache, merge_sources
from src.core.utils.time import datetime_now
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.di.providers.retort import RetortProvider
from src.infrastructure.services.s.device_scan import DeviceScan, UserDevices
from src.infrastructure.services.s.reminders import (
    DeviceReminders,
    collect_outdated,
    collect_stale,
)


@pytest.fixture(scope="module")
def hub() -> TranslatorHub:
    container = make_container(
        RetortProvider(), I18nProvider(), context={AppConfig: AppConfig.get()}, skip_validation=True
    )
    container.get(Retort)
    return container.get(TranslatorHub)


class FakeRedis:
    def __init__(self) -> None:
        self.data: Dict[str, str] = {}

    async def exists(self, *names: str) -> int:
        return sum(1 for n in names if n in self.data)

    async def set(self, name: str, value: str, ex: Optional[int] = None) -> bool:
        self.data[name] = value
        return True


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    async def notify_user(self, user: Any, payload: Any = None, i18n_key: Any = None) -> None:
        self.sent.append((user, payload))


def a_device(hwid: str, ua: str, *, model: str = "Pixel 8", days_ago: float = 0) -> HwidDeviceDto:
    moment = datetime_now() - timedelta(days=days_ago)
    return HwidDeviceDto.model_validate(
        {
            "hwid": hwid,
            "userId": 1,
            "platform": "Android",
            "osVersion": "14",
            "deviceModel": model,
            "userAgent": ua,
            "createdAt": moment.isoformat(),
            "updatedAt": moment.isoformat(),
        }
    )


def a_scan(*devices: HwidDeviceDto, user_id: int = 7) -> DeviceScan:
    user = UserDto(id=user_id, telegram_id=1000 + user_id, name="Test")
    return DeviceScan(
        found=[UserDevices(user=user, subscription=a_subscription(), devices=list(devices))],
        failed=0,
    )


def versions(**channels: str) -> VersionsCache:
    cache = VersionsCache()
    for name, version in channels.items():
        app, _, key = name.partition("_")
        cache.channels[f"{app}/{key}"] = ChannelState(version=version, status="ok")
    return cache


SETTINGS = ClientVersionsSettingsDto()


class TestOutdated:
    def test_an_outdated_app_is_found(self) -> None:
        scan = a_scan(a_device("h1", "Happ/4.1.0/Android/1"))
        targets = collect_outdated(
            scan, versions(happ_android="4.3.0"), merge_sources({}), SETTINGS, []
        )
        assert [len(t.items) for t in targets] == [1]

    def test_a_patch_gap_is_left_alone(self) -> None:
        scan = a_scan(a_device("h1", "Happ/4.3.0/Android/1"))
        targets = collect_outdated(
            scan, versions(happ_android="4.3.2"), merge_sources({}), SETTINGS, []
        )
        assert targets == []

    def test_a_router_is_left_alone(self) -> None:
        scan = a_scan(a_device("router-1", "Happ/4.1.0/Android/1"))
        targets = collect_outdated(
            scan, versions(happ_android="4.3.0"), merge_sources({}), SETTINGS, ["router-1"]
        )
        assert targets == []

    def test_the_message_names_the_device_the_gap_and_the_interval(
        self, hub: TranslatorHub
    ) -> None:
        scan = a_scan(a_device("h1", "Happ/4.1.0/Android/1", model="Pixel <8>"))
        target = collect_outdated(
            scan, versions(happ_android="4.3.0"), merge_sources({}), SETTINGS, []
        )[0]
        payload = DeviceReminders(FakeRedis(), RecordingNotifier(), hub).render_outdated(target, 7)  # type: ignore[arg-type]

        text = hub.get_translator_by_locale(target.user.language).get(
            payload.i18n_key, **payload.i18n_kwargs
        )
        assert "Pixel &lt;8&gt;" in text
        assert "4.1.0 → <b>4.3.0</b>" in text
        assert "отдельным приложением" in text  # the GitHub APK is a sideload
        assert "раза в 7 дней" in text


class TestStale:
    def test_only_devices_past_the_threshold(self) -> None:
        scan = a_scan(
            a_device("fresh", "Happ/4.3.0/Android/1", days_ago=1),
            a_device("old", "INCY/3.5.5/android", days_ago=5),
        )
        targets = collect_stale(scan, stale_hours=72, router_hwids=[])
        assert [[i.device.hwid for i in t.items] for t in targets] == [["old"]]

    def test_the_message_renders(self, hub: TranslatorHub) -> None:
        scan = a_scan(
            a_device("old", "INCY/3.5.5/android", days_ago=20),
            a_device("other", "v2rayNG/1.8", model="Router", days_ago=4),
        )
        target = collect_stale(scan, stale_hours=72, router_hwids=[])[0]
        payload = DeviceReminders(FakeRedis(), RecordingNotifier(), hub).render_stale(target, 3)  # type: ignore[arg-type]

        text = hub.get_translator_by_locale(target.user.language).get(
            payload.i18n_key, **payload.i18n_kwargs
        )
        assert "Pixel 8</b> · INCY — не обновлялась 20 дней" in text
        assert "Router</b> — не обновлялась 4 дня" in text
        assert "освободить место" in text  # 20 days: probably abandoned
        assert "раза в 3 дня" in text


class TestInterval:
    async def test_a_device_is_reminded_about_once_per_interval(self, hub: TranslatorHub) -> None:
        redis, notifier = FakeRedis(), RecordingNotifier()
        reminders = DeviceReminders(redis, notifier, hub)  # type: ignore[arg-type]
        targets = collect_stale(a_scan(a_device("old", "INCY/3.5.5/android", days_ago=5)), 72, [])

        first = await reminders.send_stale(targets, 7)
        second = await reminders.send_stale(targets, 7)

        assert (first.sent, second.sent, second.skipped) == (1, 0, 1)
        assert len(notifier.sent) == 1

    async def test_only_the_devices_not_yet_reminded_about_are_listed(
        self, hub: TranslatorHub
    ) -> None:
        redis, notifier = FakeRedis(), RecordingNotifier()
        reminders = DeviceReminders(redis, notifier, hub)  # type: ignore[arg-type]
        one = a_device("a", "INCY/3.5.5/android", days_ago=5)
        both = [one, a_device("b", "Happ/4.1.0/Android/1", days_ago=6)]

        await reminders.send_stale(collect_stale(a_scan(one), 72, []), 7)
        await reminders.send_stale(collect_stale(a_scan(*both), 72, []), 7)

        listed = notifier.sent[-1][1].i18n_kwargs["devices"]
        assert "не обновлялась 6 дней" in listed
        assert "5 дней" not in listed

    async def test_a_manual_send_ignores_the_interval(self, hub: TranslatorHub) -> None:
        redis, notifier = FakeRedis(), RecordingNotifier()
        reminders = DeviceReminders(redis, notifier, hub)  # type: ignore[arg-type]
        targets = collect_stale(a_scan(a_device("x", "INCY/3.5.5/android", days_ago=5)), 72, [])

        await reminders.send_stale(targets, 7)
        report = await reminders.send_stale(targets, 7, respect_interval=False)

        assert report.sent == 1
        assert len(notifier.sent) == 2
