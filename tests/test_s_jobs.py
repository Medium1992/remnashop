"""The admin's text summary: worst first, cut to what Telegram accepts."""

from typing import Any

import pytest
from dishka import make_container
from test_s_reminders import a_device, a_scan

from src.application.common import TranslatorHub
from src.application.dto import UserDto
from src.core.config import AppConfig
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.di.providers.retort import RetortProvider
from src.infrastructure.services.s.jobs import CHUNK_LIMIT, DeviceJobs, ReminderKind
from src.infrastructure.services.s.reminders import collect_stale


@pytest.fixture(scope="module")
def hub() -> TranslatorHub:
    container = make_container(
        RetortProvider(), I18nProvider(), context={AppConfig: AppConfig.get()}, skip_validation=True
    )
    return container.get(TranslatorHub)


def jobs(hub: TranslatorHub) -> DeviceJobs:
    return DeviceJobs(None, None, None, None, None, None, hub)  # type: ignore[arg-type]


ADMIN = UserDto(id=1, telegram_id=1, name="Admin")


def test_a_long_summary_is_split_under_the_limit(hub: TranslatorHub) -> None:
    targets: list[Any] = []
    for user_id in range(300):
        scan = a_scan(a_device(f"h{user_id}", "INCY/3.5.5/android", days_ago=5), user_id=user_id)
        targets += collect_stale(scan, 72, [])

    chunks = jobs(hub).summary(ADMIN, ReminderKind.STALE, targets, unavailable=2)

    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_LIMIT for chunk in chunks)
    assert "Пользователей: <b>300</b>" in chunks[0]
    assert "панель не ответила по 2" in chunks[0]


def test_nothing_to_report_is_still_one_message(hub: TranslatorHub) -> None:
    chunks = jobs(hub).summary(ADMIN, ReminderKind.VERSIONS, [], unavailable=0)
    assert len(chunks) == 1
    assert "Устаревшие приложения" in chunks[0]
