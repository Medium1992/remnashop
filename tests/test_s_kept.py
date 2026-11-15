"""Hand-set devices, traffic and squads kept on renewal."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

import pytest
from dishka import make_container
from remnapy.enums import TrafficLimitStrategy

from src.application.common import TranslatorHub
from src.application.dto import PlanSnapshotDto, SettingsDto, SubscriptionDto, UserDto
from src.application.dto.s_kept import KeptLimitsDto
from src.application.services.s_kept import KeptLimits, KeptLimitsDroppedEvent, renew_from_plan
from src.application.use_cases.subscription.commands.purchase import (
    PurchaseSubscription,
    PurchaseSubscriptionDto,
)
from src.core.config import AppConfig
from src.core.enums import PlanType, PurchaseType
from src.core.utils.time import datetime_now
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.di.providers.retort import RetortProvider

USER = UserDto(id=7, telegram_id=7, name="Client")
PLAN_SQUAD, OWN_SQUAD, OWN_EXTERNAL = uuid4(), uuid4(), uuid4()


def plan() -> PlanSnapshotDto:
    return PlanSnapshotDto(
        id=1,
        name="plan",
        type=PlanType.BOTH,
        traffic_limit=100,
        device_limit=5,
        duration=30,
        tag="PLAN",
        traffic_limit_strategy=TrafficLimitStrategy.MONTH,
        internal_squads=[PLAN_SQUAD],
        external_squad=None,
    )


def hand_set(subscription_id: int = 5) -> SubscriptionDto:
    """A subscription of ``plan()`` an admin changed in all three."""
    return SubscriptionDto(
        id=subscription_id,
        user_id=USER.id,
        user_remna_id=uuid4(),
        traffic_limit=300,
        device_limit=6,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        tag="OLD",
        internal_squads=[OWN_SQUAD],
        external_squad=OWN_EXTERNAL,
        expire_at=datetime_now() + timedelta(days=3),
        url="https://sub/x",
        plan_snapshot=plan(),
    )


def limits(s: SubscriptionDto) -> tuple[Any, ...]:
    return (
        s.device_limit,
        s.traffic_limit,
        s.traffic_limit_strategy,
        s.internal_squads,
        s.external_squad,
        s.tag,
    )


class TestRenewal:
    def test_nothing_kept_takes_all_from_the_plan_as_upstream(self) -> None:
        s = hand_set()
        renew_from_plan(s, plan(), KeptLimitsDto())
        assert limits(s) == (5, 100, TrafficLimitStrategy.MONTH, [PLAN_SQUAD], None, "PLAN")

    def test_kept_devices_stay(self) -> None:
        s = hand_set()
        renew_from_plan(s, plan(), KeptLimitsDto(devices=True))
        assert limits(s) == (6, 100, TrafficLimitStrategy.MONTH, [PLAN_SQUAD], None, "PLAN")

    def test_kept_traffic_stays_with_its_strategy(self) -> None:
        s = hand_set()
        renew_from_plan(s, plan(), KeptLimitsDto(traffic=True))
        assert limits(s) == (5, 300, TrafficLimitStrategy.NO_RESET, [PLAN_SQUAD], None, "PLAN")

    def test_kept_squads_stay_both(self) -> None:
        s = hand_set()
        renew_from_plan(s, plan(), KeptLimitsDto(squads=True))
        assert limits(s) == (5, 100, TrafficLimitStrategy.MONTH, [OWN_SQUAD], OWN_EXTERNAL, "PLAN")


class Settings:
    def __init__(self, enabled: bool) -> None:
        self.value = SettingsDto()
        self.value.extra.s.kept.enabled = enabled

    async def get(self) -> SettingsDto:
        return self.value


class Dao:
    def __init__(self, **rows: KeptLimitsDto) -> None:
        self.rows = {int(k[1:]): v for k, v in rows.items()}

    async def get(self, subscription_id: int) -> KeptLimitsDto:
        return self.rows.get(subscription_id, KeptLimitsDto())

    async def set(self, subscription_id: int, name: str, value: bool) -> None:
        current = self.rows.get(subscription_id, KeptLimitsDto())
        self.rows[subscription_id] = KeptLimitsDto(**{**current.__dict__, name: value})


class Subscriptions:
    def __init__(self, subscription: Optional[SubscriptionDto]) -> None:
        self.subscription = subscription

    async def get_current(self, user_id: int) -> Optional[SubscriptionDto]:
        return self.subscription


class Uow:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> "Uow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class Events:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.sent.append(event)


def service(
    enabled: bool = True,
    dao: Optional[Dao] = None,
    current: Optional[SubscriptionDto] = None,
) -> tuple[KeptLimits, Dao, Uow, Events]:
    dao, uow, events = dao or Dao(), Uow(), Events()
    kept = KeptLimits(
        uow,  # type: ignore[arg-type]
        dao,  # type: ignore[arg-type]
        Settings(enabled),  # type: ignore[arg-type]
        Subscriptions(current),  # type: ignore[arg-type]
        events,
    )
    return kept, dao, uow, events


class TestService:
    async def test_off_nothing_is_kept_whatever_was_marked(self) -> None:
        kept, *_ = service(enabled=False, dao=Dao(s5=KeptLimitsDto(devices=True)))
        assert await kept.of(hand_set()) == KeptLimitsDto()

    async def test_on_the_marks_of_the_subscription(self) -> None:
        kept, *_ = service(dao=Dao(s5=KeptLimitsDto(devices=True)))
        assert await kept.of(hand_set()) == KeptLimitsDto(devices=True)
        assert await kept.of(hand_set(subscription_id=6)) == KeptLimitsDto()
        assert await kept.of(None) == KeptLimitsDto()

    async def test_toggle_flips_the_mark_of_the_current_subscription(self) -> None:
        kept, dao, uow, _ = service(current=hand_set())
        assert await kept.toggle(USER.id, "traffic") is True
        assert dao.rows[5] == KeptLimitsDto(traffic=True)
        assert await kept.toggle(USER.id, "traffic") is False
        assert uow.commits == 2

    async def test_toggle_without_a_subscription(self) -> None:
        kept, dao, uow, _ = service(current=None)
        assert await kept.toggle(USER.id, "devices") is None
        assert not dao.rows and not uow.commits

    async def test_a_plan_change_tells_the_admins_what_was_dropped(self) -> None:
        kept, _, _, events = service(dao=Dao(s5=KeptLimitsDto(devices=True, squads=True)))
        await kept.dropped(USER, hand_set())
        (event,) = events.sent
        assert isinstance(event, KeptLimitsDroppedEvent)
        assert (event.devices, event.traffic, event.squads) == (1, 0, 1)
        assert (event.device_limit, event.traffic_limit, event.user_id) == (6, 300, USER.id)

    @pytest.mark.parametrize("enabled, marks", [(True, KeptLimitsDto()), (False, None)])
    async def test_nothing_kept_nothing_told(
        self, enabled: bool, marks: Optional[KeptLimitsDto]
    ) -> None:
        dao = Dao(s5=marks or KeptLimitsDto(devices=True))
        kept, _, _, events = service(enabled=enabled, dao=dao)
        await kept.dropped(USER, hand_set())
        assert not events.sent


@pytest.fixture(scope="module")
def i18n() -> Any:
    container = make_container(
        RetortProvider(), I18nProvider(), context={AppConfig: AppConfig.get()}, skip_validation=True
    )
    return container.get(TranslatorHub).get_translator_by_locale("ru")


class Broken:
    async def get(self, *args: Any) -> Any:
        raise RuntimeError("no table")

    async def publish(self, event: Any) -> None:
        raise RuntimeError("queue is down")


class TestNeverBreaksAPayment:
    async def test_marks_not_read_take_all_from_the_plan(self) -> None:
        kept, *_ = service(dao=Broken())  # type: ignore[arg-type]
        assert await kept.of(hand_set()) == KeptLimitsDto()

    async def test_settings_not_read_take_all_from_the_plan(self) -> None:
        kept, *_ = service()
        kept.settings_dao = Broken()  # type: ignore[assignment]
        assert await kept.of(hand_set()) == KeptLimitsDto()

    async def test_the_admins_not_told_is_only_logged(self) -> None:
        kept, *_ = service(dao=Dao(s5=KeptLimitsDto(devices=True)))
        kept.event_publisher = Broken()  # type: ignore[assignment]
        await kept.dropped(USER, hand_set())


def test_the_dropped_event_renders(i18n: Any) -> None:
    event = KeptLimitsDroppedEvent(
        user_id=7, telegram_id=7, name="Client", devices=1, traffic=1, device_limit=6
    )
    text = i18n.get(event.event_key, **event.as_payload().i18n_kwargs)
    assert "устройства — 6, трафик — без лимита" in text


@pytest.mark.parametrize(
    "devices, traffic, squads, listed",
    [
        (1, 0, 0, "устройства."),
        (0, 1, 1, "трафик, сквады."),
        (1, 1, 1, "устройства, трафик, сквады."),
        (0, 0, 1, "сквады."),
    ],
)
def test_the_card_lists_what_is_kept(
    i18n: Any, devices: int, traffic: int, squads: int, listed: str
) -> None:
    text = i18n.get(
        "msg-s-kept.card", s_kept_devices=devices, s_kept_traffic=traffic, s_kept_squads=squads
    )
    assert f"сохраняется:</b> {listed}" in text


def test_the_user_note_renders(i18n: Any) -> None:
    assert "индивидуально" in i18n.get("msg-s-kept.user")


class Panel:
    def __init__(self) -> None:
        self.written: list[SubscriptionDto] = []

    async def update_user(self, **kwargs: Any) -> None:
        self.written.append(kwargs["subscription"])


class Nothing:
    async def update(self, value: Any) -> None:
        return None


@pytest.mark.parametrize("marks, devices", [(KeptLimitsDto(devices=True), 6), (None, 5)])
async def test_a_renewal_keeps_what_the_admin_kept(
    marks: Optional[KeptLimitsDto], devices: int
) -> None:
    kept, *_ = service(dao=Dao(s5=marks or KeptLimitsDto()))
    panel = Panel()
    purchase = PurchaseSubscription(
        Uow(),  # type: ignore[arg-type]
        Nothing(),  # type: ignore[arg-type]
        Nothing(),  # type: ignore[arg-type]
        panel,  # type: ignore[arg-type]
        Settings(True),  # type: ignore[arg-type]
        kept,
    )
    transaction = SimpleNamespace(id=1, plan_snapshot=plan(), purchase_type=PurchaseType.RENEW)
    await purchase.system(
        PurchaseSubscriptionDto(user=USER, transaction=transaction, subscription=hand_set())  # type: ignore[arg-type]
    )
    (written,) = panel.written
    assert (written.device_limit, written.traffic_limit) == (devices, 100)
