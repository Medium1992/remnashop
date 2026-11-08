"""Deleting lapsed profiles: never without a warning that had time to land."""

from datetime import timedelta
from typing import Any, Dict, List, Optional

from dishka.integrations.aiogram import CONTAINER_NAME
from test_service_end_to_end import a_subscription

from src.application.common import TranslatorRunner
from src.application.dto import SettingsDto, UserDto
from src.core.enums import SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.services.s.inactive import Inactive, InactiveProfiles, inactive_since
from src.telegram.routers.s.subscription import subscription_extra


class FakeRedis:
    def __init__(self) -> None:
        self.data: Dict[str, str] = {}

    async def get(self, name: str) -> Optional[str]:
        return self.data.get(name)

    async def set(self, name: str, value: str, ex: Optional[int] = None) -> bool:
        self.data[name] = value
        return True

    async def delete(self, *names: str) -> int:
        return sum(1 for n in names if self.data.pop(n, None) is not None)


class FakeSettingsDao:
    def __init__(self, warn_days: int = 3, delete_after_days: int = 30) -> None:
        self.settings = SettingsDto()
        inactive = self.settings.extra.s.inactive
        inactive.enabled = True
        inactive.warn_days = warn_days
        inactive.delete_after_days = delete_after_days

    async def get(self) -> SettingsDto:
        return self.settings


class RecordingNotifier:
    def __init__(self) -> None:
        self.keys: List[str] = []

    async def notify_user(self, user: Any, payload: Any = None, i18n_key: Any = None) -> None:
        self.keys.append(payload.i18n_key)


class RecordingDelete:
    def __init__(self) -> None:
        self.deleted: List[int] = []

    @property
    def system(self) -> Any:
        async def run(user_id: int) -> None:
            self.deleted.append(user_id)

        return run


def lapsed(days_ago: float, user_id: int = 7, sub_id: int = 70) -> Inactive:
    since = datetime_now() - timedelta(days=days_ago)
    subscription = a_subscription(status=SubscriptionStatus.EXPIRED, expire_at=since)
    subscription.id = sub_id
    return Inactive(
        user=UserDto(id=user_id, telegram_id=1000 + user_id, name="Test"),
        subscription=subscription,
        since=since,
    )


def build(items: List[Inactive], **settings: int) -> tuple[InactiveProfiles, Any, Any, Any]:
    redis, notifier, delete = FakeRedis(), RecordingNotifier(), RecordingDelete()
    service = InactiveProfiles(
        session=None,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        user_dao=None,  # type: ignore[arg-type]
        subscription_dao=None,  # type: ignore[arg-type]
        settings_dao=FakeSettingsDao(**settings),  # type: ignore[arg-type]
        remnawave=None,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        delete_subscription=delete,  # type: ignore[arg-type]
    )

    async def candidates(include_disabled: bool) -> List[Inactive]:
        return items

    service.candidates = candidates  # type: ignore[method-assign]
    return service, redis, notifier, delete


async def warned(
    service: InactiveProfiles, redis: FakeRedis, item: Inactive, days_ago: float
) -> None:
    redis.data[service._warning_key(item)] = (datetime_now() - timedelta(days=days_ago)).isoformat()


class TestDecisions:
    async def test_too_early_for_anything(self) -> None:
        service, _, notifier, delete = build([lapsed(10)])
        report = await service.run()
        assert (report.warned, report.deleted) == (0, 0)
        assert notifier.keys == []

    async def test_a_warning_goes_out_ahead_of_the_date(self) -> None:
        service, _, notifier, delete = build([lapsed(28)])
        report = await service.run()
        assert (report.warned, report.deleted) == (1, 0)
        assert notifier.keys == ["ntf-s-inactive.warning"]

    async def test_a_backlog_is_warned_first_not_deleted_on_the_spot(self) -> None:
        # Switching the feature on with users lapsed long ago.
        service, _, notifier, delete = build([lapsed(400)])
        report = await service.run()
        assert (report.warned, report.deleted) == (1, 0)
        assert delete.deleted == []

    async def test_deleted_once_the_warning_had_its_days(self) -> None:
        item = lapsed(40)
        service, redis, notifier, delete = build([item])
        await warned(service, redis, item, days_ago=4)

        report = await service.run()

        assert report.deleted == 1
        assert delete.deleted == [7]
        assert notifier.keys == ["ntf-s-inactive.deleted"]

    async def test_not_deleted_while_the_warning_is_fresh(self) -> None:
        item = lapsed(40)
        service, redis, _, delete = build([item])
        await warned(service, redis, item, days_ago=1)

        assert (await service.run()).deleted == 0
        assert delete.deleted == []

    async def test_without_warnings_the_date_alone_decides(self) -> None:
        service, _, notifier, delete = build([lapsed(31)], warn_days=0)
        assert (await service.run()).deleted == 1
        assert notifier.keys == ["ntf-s-inactive.deleted"]

    async def test_an_excluded_user_is_left_alone(self) -> None:
        service, _, _, delete = build([lapsed(400)], warn_days=0)
        assert (await service.run(excluded_user_ids={7})).deleted == 0

    async def test_a_new_lapse_needs_a_new_warning(self) -> None:
        # Warned once, renewed, lapsed again: the old warning does not count.
        old = lapsed(80)
        current = lapsed(40)
        service, redis, notifier, delete = build([current])
        await warned(service, redis, old, days_ago=50)

        report = await service.run()

        assert (report.warned, report.deleted) == (1, 0)

    async def test_off_does_nothing(self) -> None:
        service, _, _, delete = build([lapsed(400)], warn_days=0)
        service.settings_dao.settings.extra.s.inactive.enabled = False  # type: ignore[attr-defined]
        assert (await service.run()).candidates == 0


class TestSince:
    def test_active_is_not_inactive(self) -> None:
        sub = a_subscription(expire_at=datetime_now() + timedelta(days=5))
        assert inactive_since(sub, datetime_now()) is None

    def test_an_expired_date_counts_even_if_the_status_lags(self) -> None:
        past = datetime_now() - timedelta(days=2)
        sub = a_subscription(status=SubscriptionStatus.ACTIVE, expire_at=past)
        assert inactive_since(sub, datetime_now()) == past


class FakeContainer:
    def __init__(self, settings: FakeSettingsDao, i18n: Any) -> None:
        self.settings, self.i18n = settings, i18n

    async def get(self, kind: Any) -> Any:
        return self.i18n if kind is TranslatorRunner else self.settings


class EchoI18n:
    def get(self, key: str, **kwargs: Any) -> str:
        return f"{key}:{kwargs['date']}"


class TestSubscriptionScreen:
    async def _extra(self, subscription: Any, **settings: Any) -> Any:
        dao = FakeSettingsDao(**settings)

        class Manager:
            middleware_data = {CONTAINER_NAME: FakeContainer(dao, EchoI18n())}

        return await subscription_extra(Manager(), subscription)  # type: ignore[arg-type]

    async def test_a_lapsed_subscription_shows_its_deletion_date(self) -> None:
        since = datetime_now() - timedelta(days=5)
        sub = a_subscription(status=SubscriptionStatus.EXPIRED, expire_at=since)

        extra = await self._extra(sub, delete_after_days=30)

        expected = (since + timedelta(days=30)).strftime("%d.%m.%Y")
        assert extra["s_inactive"] == f"msg-s-subscription.inactive:{expected}"

    async def test_an_active_one_shows_nothing(self) -> None:
        sub = a_subscription(expire_at=datetime_now() + timedelta(days=5))
        assert (await self._extra(sub))["s_inactive"] == ""
