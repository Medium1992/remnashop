"""Deleting lapsed profiles: never without a warning that had time to land."""

from datetime import timedelta
from typing import Any, Dict, List, Optional

from dishka.integrations.aiogram import CONTAINER_NAME
from test_service_end_to_end import a_subscription

from src.application.common import TranslatorRunner
from src.application.dto import SettingsDto, UserDto
from src.core.enums import SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.services.s.blocks import BlockService
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
    def __init__(
        self, warn_days: int = 3, delete_after_days: int = 30, disable_instead: bool = False
    ) -> None:
        self.settings = SettingsDto()
        inactive = self.settings.extra.s.inactive
        inactive.enabled = True
        inactive.warn_days = warn_days
        inactive.delete_after_days = delete_after_days
        inactive.disable_instead = disable_instead

    async def get(self) -> SettingsDto:
        return self.settings


class RecordingNotifier:
    def __init__(self) -> None:
        self.keys: List[str] = []

    async def notify_user(self, user: Any, payload: Any = None, i18n_key: Any = None) -> None:
        self.keys.append(payload.i18n_key)


class FakeBlocks:
    def __init__(self, blocked: Optional[set] = None) -> None:
        self.blocked = blocked or set()

    async def user_ids(self) -> set:
        return self.blocked

    async def get(self, user_id: int) -> None:
        return None


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


class Uow:
    async def __aenter__(self) -> "Uow":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class Panel:
    def __init__(self) -> None:
        self.calls: List[tuple[str, Any]] = []

    async def disable_user(self, uuid: Any) -> None:
        self.calls.append(("disable", uuid))

    async def enable_user(self, uuid: Any) -> None:
        self.calls.append(("enable", uuid))


class Subscriptions:
    def __init__(self, current: Any = None) -> None:
        self.current = current
        self.statuses: List[tuple[int, SubscriptionStatus]] = []

    async def get_current(self, user_id: int) -> Any:
        return self.current

    async def update_status(self, subscription_id: int, status: SubscriptionStatus) -> None:
        self.statuses.append((subscription_id, status))
        if self.current is not None:
            self.current.status = status


class Noted:
    def __init__(self) -> None:
        self.rows: Dict[Any, int] = {}

    async def add(self, uuid: Any, user_id: int) -> None:
        self.rows[uuid] = user_id

    async def find(self, uuid: Any) -> Optional[int]:
        return self.rows.get(uuid)

    async def remove(self, uuid: Any) -> None:
        self.rows.pop(uuid, None)


class Users:
    async def get_by_id(self, user_id: int) -> Any:
        return UserDto(id=user_id, telegram_id=1000 + user_id, name="Test")


def build(items: List[Inactive], **settings: Any) -> tuple[InactiveProfiles, Any, Any, Any]:
    redis, notifier, delete = FakeRedis(), RecordingNotifier(), RecordingDelete()
    service = InactiveProfiles(
        session=None,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        user_dao=Users(),  # type: ignore[arg-type]
        subscription_dao=Subscriptions(),  # type: ignore[arg-type]
        settings_dao=FakeSettingsDao(**settings),  # type: ignore[arg-type]
        remnawave=Panel(),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        delete_subscription=delete,  # type: ignore[arg-type]
        block_dao=FakeBlocks(),  # type: ignore[arg-type]
        uow=Uow(),  # type: ignore[arg-type]
        rule_disabled_dao=Noted(),  # type: ignore[arg-type]
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

    async def test_a_blocked_user_is_left_alone(self) -> None:
        service, _, _, delete = build([lapsed(400)], warn_days=0)
        service.block_dao = FakeBlocks({7})  # type: ignore[assignment]
        assert (await service.run()).deleted == 0

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
        if kind is BlockService:
            return FakeBlocks()
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

        delete_on = since + timedelta(days=30)
        # Telegram shows the date in the reader's format; old clients see the plain one.
        expected = (
            f'<tg-time unix="{int(delete_on.timestamp())}" format="D">'
            f"{delete_on.strftime('%d.%m.%Y')}</tg-time>"
        )
        assert extra["s_inactive"] == f"msg-s-subscription.inactive:{expected}"

    async def test_an_active_one_shows_nothing(self) -> None:
        sub = a_subscription(expire_at=datetime_now() + timedelta(days=5))
        assert (await self._extra(sub))["s_inactive"] == ""


class TestSwitchingOff:
    async def test_the_warning_and_the_action_say_switch_off(self) -> None:
        item = lapsed(400)
        service, redis, notifier, delete = build([item], disable_instead=True)
        await service.run()
        assert notifier.keys == ["ntf-s-inactive.warning-disable"]
        await warned(service, redis, item, days_ago=4)
        report = await service.run()
        assert (report.disabled, report.deleted) == (1, 0) and not delete.deleted
        panel, noted = service.remnawave, service.rule_disabled_dao
        assert panel.calls == [("disable", item.subscription.user_remna_id)]  # type: ignore[attr-defined]
        assert noted.rows == {item.subscription.user_remna_id: 7}  # type: ignore[attr-defined]
        assert service.subscription_dao.statuses == [(70, SubscriptionStatus.DISABLED)]  # type: ignore[attr-defined]
        assert notifier.keys[-1] == "ntf-s-inactive.disabled"

    async def test_a_disabled_profile_is_not_taken_again(self) -> None:
        service, *_ = build([], disable_instead=True, delete_after_days=30)
        seen: List[bool] = []

        async def candidates(include_disabled: bool) -> List[Inactive]:
            seen.append(include_disabled)
            return []

        service.candidates = candidates  # type: ignore[method-assign]
        service.settings_dao.settings.extra.s.inactive.include_disabled = True  # type: ignore[attr-defined]
        await service.run()
        assert seen == [False]


def purchase(user_id: int = 7) -> Any:
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(user_id=user_id)


class TestRenewal:
    def _renewed(self, status: SubscriptionStatus, noted: bool) -> tuple[InactiveProfiles, Any]:
        item = lapsed(400)
        item.subscription.status = status
        service, *_ = build([], disable_instead=True)
        service.subscription_dao.current = item.subscription  # type: ignore[attr-defined]
        if noted:
            service.rule_disabled_dao.rows[item.subscription.user_remna_id] = 7  # type: ignore[attr-defined]
        return service, item

    async def test_a_renewal_switches_the_rule_s_profile_back_on(self) -> None:
        service, item = self._renewed(SubscriptionStatus.DISABLED, noted=True)
        await service.on_purchase(purchase())  # type: ignore[arg-type]
        assert service.remnawave.calls == [("enable", item.subscription.user_remna_id)]  # type: ignore[attr-defined]
        assert item.subscription.status == SubscriptionStatus.ACTIVE
        assert service.rule_disabled_dao.rows == {}  # type: ignore[attr-defined]

    async def test_one_the_admin_switched_off_stays_off(self) -> None:
        service, item = self._renewed(SubscriptionStatus.DISABLED, noted=False)
        await service.on_purchase(purchase())  # type: ignore[arg-type]
        assert service.remnawave.calls == []  # type: ignore[attr-defined]
        assert item.subscription.status == SubscriptionStatus.DISABLED

    async def test_a_blocked_one_waits_for_the_block(self) -> None:
        service, item = self._renewed(SubscriptionStatus.DISABLED, noted=True)
        service.block_dao.blocked = {7}  # type: ignore[attr-defined]
        await service.on_purchase(purchase())  # type: ignore[arg-type]
        assert service.remnawave.calls == []  # type: ignore[attr-defined]
        assert service.rule_disabled_dao.rows  # type: ignore[attr-defined]

    async def test_the_note_goes_when_the_profile_is_already_on(self) -> None:
        service, item = self._renewed(SubscriptionStatus.ACTIVE, noted=True)
        await service.on_purchase(purchase())  # type: ignore[arg-type]
        assert service.remnawave.calls == [] and service.rule_disabled_dao.rows == {}  # type: ignore[attr-defined]
