"""A plan change with what is left of the current plan counted, and the devices
that do not fit removed before the lower limit reaches the panel."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

import pytest
from adaptix import Retort
from dishka import make_async_container
from remnapy.enums import TrafficLimitStrategy

from src.application.common import Notifier, TranslatorRunner
from src.application.common.dao import SettingsDao
from src.application.dto import (
    PlanDto,
    PriceDetailsDto,
    SettingsDto,
    SubscriptionDto,
    TransactionDto,
    UserDto,
)
from src.application.dto.plan import PlanDurationDto, PlanPriceDto, PlanSnapshotDto
from src.application.dto.s_plan_change import ChangeMode, PlanChangeDto
from src.application.services import PricingService
from src.application.services.s_plan_change import (
    Leftover,
    PlanChange,
    devices_to_drop,
    keeps_expiry,
    leftover_of,
    quote,
)
from src.application.use_cases.gateways.commands.payment import CreatePayment, CreatePaymentDto
from src.application.use_cases.subscription.commands.purchase import (
    PurchaseSubscription,
    PurchaseSubscriptionDto,
)
from src.core.constants import USER_KEY
from src.core.enums import (
    Currency,
    PaymentGatewayType,
    PlanType,
    PurchaseType,
    SubscriptionStatus,
    TransactionStatus,
)
from src.infrastructure.di.providers.retort import RetortProvider
from src.telegram.routers.s import plan_change as dialog
from src.telegram.routers.subscription.dialog import router as subscription_dialog
from src.telegram.states import Subscription

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
RUB = Currency.RUB
USER = UserDto(id=7, telegram_id=7, name="Buyer")
pricing = PricingService()


def rub(amount: Decimal) -> Decimal:
    return pricing.apply_currency_rules(amount, RUB)


def snapshot(plan_id: int = 1, duration: int = 30, devices: int = 3) -> PlanSnapshotDto:
    return PlanSnapshotDto(
        id=plan_id,
        name=f"plan-{plan_id}",
        type=PlanType.BOTH,
        traffic_limit=100,
        device_limit=devices,
        duration=duration,
    )


def subscription(days_left: float = 25, **kwargs: Any) -> SubscriptionDto:
    return SubscriptionDto(
        id=5,
        user_id=USER.id,
        user_remna_id=uuid4(),
        traffic_limit=100,
        device_limit=3,
        # A traffic that resets is only a cap: the days are what was bought.
        traffic_limit_strategy=TrafficLimitStrategy.MONTH,
        expire_at=NOW + timedelta(days=days_left),
        url="https://sub/x",
        plan_snapshot=snapshot(),
        **kwargs,
    )


def paid(
    amount: str,
    plan_id: int = 1,
    duration: int = 30,
    currency: Currency = RUB,
    status: TransactionStatus = TransactionStatus.COMPLETED,
    change: Optional[PlanChangeDto] = None,
    is_test: bool = False,
) -> TransactionDto:
    return TransactionDto(
        payment_id=uuid4(),
        user_id=USER.id,
        status=status,
        is_test=is_test,
        purchase_type=PurchaseType.NEW,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=PriceDetailsDto(
            original_amount=Decimal(amount),
            discount_percent=0,
            final_amount=Decimal(amount),
            change=change,
        ),
        currency=currency,
        plan_snapshot=snapshot(plan_id, duration),
    )


def price(amount: str) -> PriceDetailsDto:
    return PriceDetailsDto(
        original_amount=Decimal(amount), discount_percent=0, final_amount=Decimal(amount)
    )


# ── what is left ─────────────────────────────────────────────────────────────


class TestLeftover:
    def test_a_day_is_worth_what_was_paid_for_it(self) -> None:
        left = leftover_of(subscription(25), [paid("300")], RUB, NOW)
        assert left is not None
        assert (left.daily, left.days, left.value) == (Decimal(10), Decimal(25), Decimal(250))

    def test_discounts_count(self) -> None:
        # 300 with a discount: 150 paid, a day is worth 5.
        left = leftover_of(subscription(10), [paid("150")], RUB, NOW)
        assert left is not None and left.value == Decimal(50)

    def test_the_newest_payment_for_that_plan(self) -> None:
        transactions = [paid("999", plan_id=2), paid("600"), paid("300")]
        left = leftover_of(subscription(10), transactions, RUB, NOW)
        assert left is not None and left.daily == Decimal(20)

    def test_a_change_counts_the_plan_not_the_part_paid(self) -> None:
        change = PlanChangeDto(mode=ChangeMode.KEEP_EXPIRY, plan_amount=Decimal(600))
        left = leftover_of(subscription(10), [paid("120", change=change)], RUB, NOW)
        assert left is not None and left.daily == Decimal(20)

    @pytest.mark.parametrize(
        "sub, transactions",
        [
            (None, [paid("300")]),
            (subscription(is_trial=True), [paid("300")]),
            (subscription(-1), [paid("300")]),
            (subscription(), []),  # a gift: nothing was paid
            (subscription(), [paid("0")]),  # free with a promocode
            (subscription(), [paid("300", status=TransactionStatus.PENDING)]),
            (subscription(), [paid("300", status=TransactionStatus.CANCELED)]),
            (subscription(), [paid("300", is_test=True)]),
            (subscription(), [paid("300", currency=Currency.USD)]),
            (subscription(), [paid("300", duration=0)]),
        ],
    )
    def test_nothing_to_count(
        self, sub: Optional[SubscriptionDto], transactions: list[TransactionDto]
    ) -> None:
        assert leftover_of(sub, transactions, RUB, NOW) is None

    def test_nothing_for_a_lifetime_subscription(self) -> None:
        forever = subscription()
        forever.expire_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert leftover_of(forever, [paid("300")], RUB, NOW) is None


GB = 1024**3


def bought_traffic(days_left: float = 25, limit_gb: int = 100) -> SubscriptionDto:
    sub = subscription(days_left)
    sub.traffic_limit = limit_gb
    sub.traffic_limit_strategy = TrafficLimitStrategy.NO_RESET
    return sub


class TestTrafficBought:
    def test_the_traffic_left_counts_when_it_runs_out_first(self) -> None:
        # 95 GB of 100 used in 5 days of 30: 5 % is left, not 25/30.
        left = leftover_of(bought_traffic(25), [paid("300")], RUB, NOW, used_bytes=95 * GB)
        assert left is not None
        assert left.value == Decimal(15)
        assert (left.traffic_left_gb, left.traffic_limit_gb) == (Decimal(5), 100)

    def test_the_days_count_when_they_run_out_first(self) -> None:
        # 10 GB of 100 used, 5 days of 30 left: the days are worth less.
        left = leftover_of(bought_traffic(5), [paid("300")], RUB, NOW, used_bytes=10 * GB)
        assert left is not None and left.value == Decimal(50)

    def test_a_renewal_counts_one_allowance_not_the_days_added(self) -> None:
        # Renewed early: 55 days left but one fresh 100 GB, half of it used.
        left = leftover_of(bought_traffic(55), [paid("300")], RUB, NOW, used_bytes=50 * GB)
        assert left is not None and left.value == Decimal(150)

    def test_nothing_when_all_the_traffic_is_used(self) -> None:
        assert leftover_of(bought_traffic(), [paid("300")], RUB, NOW, used_bytes=120 * GB) is None

    def test_nothing_counted_when_the_panel_does_not_say(self) -> None:
        assert leftover_of(bought_traffic(), [paid("300")], RUB, NOW) is None

    def test_the_used_traffic_does_not_matter_when_it_resets(self) -> None:
        left = leftover_of(subscription(25), [paid("300")], RUB, NOW, used_bytes=99 * GB)
        assert left is not None and left.value == Decimal(250)

    @pytest.mark.parametrize(
        "limit, strategy, keeps",
        [
            (100, TrafficLimitStrategy.NO_RESET, False),
            (100, TrafficLimitStrategy.MONTH, True),
            (100, TrafficLimitStrategy.DAY, True),
            (0, TrafficLimitStrategy.NO_RESET, True),
        ],
    )
    def test_the_date_is_kept_only_where_traffic_is_not_bought(
        self, limit: int, strategy: TrafficLimitStrategy, keeps: bool
    ) -> None:
        new = snapshot()
        new.traffic_limit, new.traffic_limit_strategy = limit, strategy
        assert keeps_expiry(new) is keeps

    def test_keeping_the_date_pays_for_the_days_less_what_is_left(self) -> None:
        # 25 days at 20 a day is 500; 5 % of a 300 plan (15) is left.
        left = Leftover(
            expire_at=NOW, days=Decimal(25), daily=Decimal(10), worth_days=Decimal("1.5")
        )
        assert quote(price("600"), 30, left, ChangeMode.KEEP_EXPIRY, rub).final_amount == 485

    async def test_the_panel_is_asked_only_where_traffic_is_bought(self) -> None:
        asked: list[Any] = []

        class Panel:
            async def get_user_by_uuid(self, uuid: Any) -> Any:
                asked.append(uuid)
                return type("User", (), {"used_traffic_bytes": 95 * GB})()

        class Subscriptions:
            def __init__(self, sub: SubscriptionDto) -> None:
                self.sub = sub

            async def get_current(self, user_id: int) -> SubscriptionDto:
                return self.sub

        class Transactions:
            async def get_by_user(self, user_id: int) -> list[TransactionDto]:
                return [paid("300")]

        sub = bought_traffic(25)
        sub.expire_at = datetime.now(timezone.utc) + timedelta(days=25)
        change = PlanChange(Subscriptions(sub), Transactions(), Panel())  # type: ignore[arg-type]
        left = await change.leftover(USER.id, RUB)
        assert asked == [sub.user_remna_id]
        assert left is not None and left.value == Decimal(15)

        asked.clear()
        sub.traffic_limit_strategy = TrafficLimitStrategy.MONTH
        assert await change.leftover(USER.id, RUB) is not None
        assert asked == []


# ── what is due ──────────────────────────────────────────────────────────────

LEFT = Leftover(expire_at=NOW + timedelta(days=25), days=Decimal(25), daily=Decimal(10))


class TestQuote:
    def test_keep_the_date_pays_the_daily_difference(self) -> None:
        # 600 for 30 days is 20 a day; 10 more a day for the 25 days left.
        due = quote(price("600"), 30, LEFT, ChangeMode.KEEP_EXPIRY, rub)
        assert due.final_amount == Decimal(250)
        assert due.change == PlanChangeDto(
            mode=ChangeMode.KEEP_EXPIRY, plan_amount=Decimal(600), credit=Decimal("0.00")
        )

    def test_from_today_takes_what_is_left_off(self) -> None:
        due = quote(price("600"), 30, LEFT, ChangeMode.FROM_TODAY, rub)
        assert due.final_amount == Decimal(350)
        assert due.original_amount == Decimal(600)
        assert due.change is not None and due.change.credit == Decimal("250.00")

    @pytest.mark.parametrize("mode", list(ChangeMode))
    def test_cheaper_is_free_and_nothing_comes_back(self, mode: ChangeMode) -> None:
        due = quote(price("150"), 30, LEFT, mode, rub)
        assert due.final_amount == 0 and due.is_free

    def test_the_same_daily_price_is_free_to_keep(self) -> None:
        assert quote(price("300"), 30, LEFT, ChangeMode.KEEP_EXPIRY, rub).is_free

    def test_rounded_as_the_currency_wants(self) -> None:
        left = Leftover(expire_at=NOW, days=Decimal("2.5"), daily=Decimal("10"))
        # (20 - 10) * 2.5 = 25; 599 / 30 - 10 = 9.966… * 2.5 = 24.9… -> 24 roubles.
        assert quote(price("599"), 30, left, ChangeMode.KEEP_EXPIRY, rub).final_amount == 24

    def test_a_discount_on_the_new_plan_stays(self) -> None:
        discounted = PriceDetailsDto(
            original_amount=Decimal(600), discount_percent=50, final_amount=Decimal(300)
        )
        due = quote(discounted, 30, LEFT, ChangeMode.FROM_TODAY, rub)
        assert (due.final_amount, due.discount_percent) == (Decimal(50), 50)


@pytest.mark.parametrize(
    "count, limit, must", [(5, 3, 2), (3, 3, 0), (1, 3, 0), (5, 0, 0), (0, 1, 0)]
)
def test_devices_that_do_not_fit(count: int, limit: int, must: int) -> None:
    assert devices_to_drop([object()] * count, limit) == must  # type: ignore[list-item]


# ── the change applied after payment ─────────────────────────────────────────


class Uow:
    async def __aenter__(self) -> "Uow":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def commit(self) -> None:
        pass


@dataclass
class PanelUser:
    id: int
    status: str
    expire_at: datetime
    subscription_url: str = "https://sub/x"


class Panel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def delete_device(self, user_uuid: Any, hwid: str) -> int:
        self.calls.append(("delete", hwid))
        return 1

    async def update_user(self, user: Any, uuid: Any, plan: Any = None, **kwargs: Any) -> Any:
        sub = kwargs.get("subscription")
        self.calls.append(("update", sub.expire_at if sub else plan.duration))
        expire = sub.expire_at if sub else NOW + timedelta(days=plan.duration)
        return PanelUser(id=1, status="ACTIVE", expire_at=expire)


class Subscriptions:
    def __init__(self) -> None:
        self.created: list[SubscriptionDto] = []

    async def update_status(self, **kwargs: Any) -> None:
        pass

    async def create(self, subscription: SubscriptionDto, user_id: int) -> None:
        self.created.append(subscription)


class Users:
    async def update(self, user: Any) -> None:
        pass


async def purchase(change: Optional[PlanChangeDto], has_trial: bool = False) -> tuple[Panel, Any]:
    panel, subs = Panel(), Subscriptions()
    use_case = PurchaseSubscription(Uow(), Users(), subs, panel, None)  # type: ignore[arg-type]
    current = subscription(25, is_trial=has_trial)
    transaction = paid("250", plan_id=2, change=change)
    transaction.purchase_type = PurchaseType.CHANGE
    await use_case._execute(USER, PurchaseSubscriptionDto(USER, transaction, current))
    return panel, (current, subs.created[0])


class TestApplied:
    async def test_keep_the_date(self) -> None:
        panel, (old, new) = await purchase(PlanChangeDto(mode=ChangeMode.KEEP_EXPIRY))
        assert panel.calls == [("update", old.expire_at)]
        assert new.expire_at == old.expire_at
        assert new.plan_snapshot.id == 2 and new.status == SubscriptionStatus.ACTIVE

    async def test_from_today(self) -> None:
        panel, (_, new) = await purchase(PlanChangeDto(mode=ChangeMode.FROM_TODAY))
        assert panel.calls == [("update", 30)]
        assert new.expire_at == NOW + timedelta(days=30)

    async def test_as_upstream_without_a_change(self) -> None:
        panel, _ = await purchase(None)
        assert panel.calls == [("update", 30)]

    async def test_the_devices_go_before_the_limit_drops(self) -> None:
        change = PlanChangeDto(mode=ChangeMode.KEEP_EXPIRY, drop_hwids=["h1", "h2"])
        panel, _ = await purchase(change)
        assert [c[0] for c in panel.calls] == ["delete", "delete", "update"]
        assert [c[1] for c in panel.calls[:2]] == ["h1", "h2"]

    async def test_a_trial_converts_as_upstream(self) -> None:
        change = PlanChangeDto(mode=ChangeMode.KEEP_EXPIRY, drop_hwids=["h1"])
        panel, _ = await purchase(change, has_trial=True)
        assert panel.calls == [("update", 30)]


# ── in the dialog ────────────────────────────────────────────────────────────


@dataclass
class Device:
    hwid: str
    device_model: Optional[str] = None
    platform: Optional[str] = None


class FakePlanChange:
    def __init__(self, left: Optional[Leftover], devices: int = 0) -> None:
        self.left = left
        self.device_list = [Device(f"h{i}", device_model=f"Phone {i}") for i in range(devices)]

    async def leftover(self, user_id: int, currency: Currency) -> Optional[Leftover]:
        return self.left if currency == RUB else None

    async def devices(self, user_id: int) -> list[Device]:
        return self.device_list


class FakeSettings:
    async def get(self) -> SettingsDto:
        return SettingsDto(default_currency=RUB)


class Checks:
    def __init__(self) -> None:
        self.checked: list[str] = []

    async def reset_checked(self) -> None:
        self.checked = []

    def get_checked(self) -> list[str]:
        return self.checked


class Container:
    def __init__(self, deps: dict[Any, Any]) -> None:
        self.deps = deps

    async def get(self, kind: Any) -> Any:
        return self.deps[kind]


class Manager:
    def __init__(self, change: FakePlanChange, purchase_type: PurchaseType, retort: Retort) -> None:
        self.notifier = FakeNotifier()
        deps = {
            PlanChange: change,
            SettingsDao: FakeSettings(),
            PricingService: pricing,
            Retort: retort,
            Notifier: self.notifier,
            TranslatorRunner: None,
        }
        self.container = Container(deps)
        self.middleware_data = {"dishka_container": self.container, USER_KEY: USER}
        self.dialog_data: dict[str, Any] = {
            "purchase_type": purchase_type,
            "payment_cache": {"x": 1},
        }
        self.checks = Checks()
        self.states: list[Any] = []

    async def switch_to(self, state: Any) -> None:
        self.states.append(state)

    def find(self, widget_id: str) -> Checks:
        return self.checks


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify_user(self, user: Any, payload: Any = None, **kwargs: Any) -> None:
        self.sent.append(payload.i18n_key)


def plan(devices: int = 3, traffic: int = 0) -> PlanDto:
    return PlanDto(
        id=2,
        name="plan-2",
        type=PlanType.BOTH if traffic else PlanType.DEVICES,
        traffic_limit=traffic,
        device_limit=devices,
        durations=[
            PlanDurationDto(days=30, prices=[PlanPriceDto(currency=RUB, price=Decimal(600))])
        ],
    )


@pytest.fixture
async def retort() -> Retort:
    container = make_async_container(RetortProvider(), skip_validation=True)
    return await container.get(Retort)


def manager(
    retort: Retort,
    left: Optional[Leftover] = LEFT,
    devices: int = 0,
    purchase_type: PurchaseType = PurchaseType.CHANGE,
    plan_devices: int = 3,
    plan_traffic: int = 0,
) -> Manager:
    m = Manager(FakePlanChange(left, devices), purchase_type, retort)
    m.dialog_data[PlanDto.__name__] = retort.dump(plan(plan_devices, plan_traffic))
    m.dialog_data["selected_duration"] = 30
    return m


class TestTheDialog:
    async def test_asks_how_to_count_what_is_left(self, retort: Retort) -> None:
        m = manager(retort)
        assert await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]
        assert m.states == [Subscription.S_CHANGE_MODE]
        # A price cached before the choice is not reused.
        assert "payment_cache" not in m.dialog_data

    async def test_asks_for_the_devices_that_do_not_fit(self, retort: Retort) -> None:
        m = manager(retort, left=None, devices=5)
        assert await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]
        assert m.states == [Subscription.S_CHANGE_DEVICES]
        assert m.dialog_data["s_change_must"] == 2

    @pytest.mark.parametrize("purchase_type", [PurchaseType.NEW, PurchaseType.RENEW])
    async def test_other_purchases_go_on_as_upstream(
        self, retort: Retort, purchase_type: PurchaseType
    ) -> None:
        m = manager(retort, devices=5, purchase_type=purchase_type)
        assert not await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]
        assert m.states == []

    async def test_nothing_to_ask(self, retort: Retort) -> None:
        m = manager(retort, left=None, devices=3)
        assert not await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]

    async def test_after_the_answers_it_goes_on(self, retort: Retort) -> None:
        m = manager(retort)
        m.dialog_data["s_change_resume"] = True
        assert not await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]

    async def test_choosing_again_starts_over(self, retort: Retort) -> None:
        m = manager(retort)
        m.dialog_data.update(s_change_mode="keep", s_change_drop=["h1"])
        await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]
        assert "s_change_mode" not in m.dialog_data and "s_change_drop" not in m.dialog_data

    async def test_the_price_as_chosen(self, retort: Retort) -> None:
        m = manager(retort)
        m.dialog_data.update(s_change_mode="today", s_change_drop=["h1"])
        priced = await dialog.change_price(m, plan(), 30, RUB, price("600"))  # type: ignore[arg-type]
        assert priced.final_amount == Decimal(350)
        assert priced.change is not None
        assert (priced.change.mode, priced.change.drop_hwids) == (ChangeMode.FROM_TODAY, ["h1"])

    async def test_another_currency_pays_in_full_from_today(self, retort: Retort) -> None:
        # Nothing is counted, so the date is not kept either.
        m = manager(retort)
        m.dialog_data["s_change_mode"] = "keep"
        priced = await dialog.change_price(m, plan(), 30, Currency.USD, price("10"))  # type: ignore[arg-type]
        assert priced.final_amount == Decimal(10) and priced.change is None

    async def test_devices_without_a_leftover(self, retort: Retort) -> None:
        m = manager(retort, left=None)
        m.dialog_data["s_change_drop"] = ["h1"]
        priced = await dialog.change_price(m, plan(), 30, RUB, price("600"))  # type: ignore[arg-type]
        assert priced.final_amount == Decimal(600)
        assert priced.change == PlanChangeDto(drop_hwids=["h1"])

    async def test_not_a_change(self, retort: Retort) -> None:
        m = manager(retort, purchase_type=PurchaseType.RENEW)
        m.dialog_data["s_change_mode"] = "today"
        priced = await dialog.change_price(m, plan(), 30, RUB, price("600"))  # type: ignore[arg-type]
        assert priced == price("600")

    async def test_too_few_devices_picked(self, retort: Retort) -> None:
        m = manager(retort, left=None, devices=5)
        await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]
        m.checks.checked = ["0"]
        await dialog.on_change_devices_done(None, None, m)  # type: ignore[arg-type]
        assert m.notifier.sent == ["ntf-s-change.devices-few"]
        assert "s_change_drop" not in m.dialog_data

    async def test_the_devices_picked_go_with_the_payment(
        self, retort: Retort, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resumed: list[int] = []

        async def resume(callback: Any, widget: Any, dialog_manager: Any) -> None:
            resumed.append(dialog_manager.dialog_data["selected_duration"])

        monkeypatch.setattr(dialog, "_resume", resume)
        m = manager(retort, left=None, devices=5)
        await dialog.intercept(m, plan(), 30)  # type: ignore[arg-type]
        m.checks.checked = ["1", "3"]
        await dialog.on_change_devices_done(None, None, m)  # type: ignore[arg-type]
        assert m.dialog_data["s_change_drop"] == ["h1", "h3"]
        assert resumed == [30]


async def test_the_price_with_its_change_keeps_through_storage(retort: Retort) -> None:
    stored = PriceDetailsDto(
        original_amount=Decimal(600),
        discount_percent=0,
        final_amount=Decimal(350),
        change=PlanChangeDto(
            mode=ChangeMode.FROM_TODAY,
            plan_amount=Decimal(600),
            credit=Decimal("250.00"),
            drop_hwids=["h1"],
        ),
    )
    assert retort.load(retort.dump(stored), PriceDetailsDto) == stored


async def test_a_price_stored_before_loads_without_a_change(retort: Retort) -> None:
    old = {"original_amount": "600", "discount_percent": 0, "final_amount": "600"}
    assert retort.load(old, PriceDetailsDto).change is None


class I18n:
    def get(self, key: str, **kwargs: Any) -> str:
        return key


async def test_the_choice_shows_both_ways_with_their_amounts(retort: Retort) -> None:
    m = manager(retort, left=Leftover(expire_at=NOW, days=Decimal("24.6"), daily=Decimal(10)))
    m.container.deps[TranslatorRunner] = I18n()
    shown = await dialog.change_mode_getter(m)  # type: ignore[arg-type]
    assert (shown["s_keep"], shown["s_today"]) == (1, 1)
    assert (shown["s_left_days"], shown["s_left_value"]) == (24, Decimal(246))
    assert (shown["s_keep_amount"], shown["s_keep_free"]) == (Decimal(246), 0)
    assert (shown["s_today_amount"], shown["s_full_amount"]) == (Decimal(354), Decimal(600))


async def test_no_keeping_the_date_for_a_plan_of_bought_traffic(retort: Retort) -> None:
    m = manager(retort, plan_traffic=100)
    m.container.deps[TranslatorRunner] = I18n()
    shown = await dialog.change_mode_getter(m)  # type: ignore[arg-type]
    assert (shown["s_keep"], shown["s_today"]) == (0, 1)

    # A choice made before still counts as a new term, never as kept.
    m.dialog_data["s_change_mode"] = ChangeMode.KEEP_EXPIRY.value
    priced = await dialog.change_price(m, plan(traffic=100), 30, RUB, price("600"))  # type: ignore[arg-type]
    assert priced.change is not None and priced.change.mode == ChangeMode.FROM_TODAY
    assert priced.final_amount == Decimal(350)


async def test_the_choice_tells_the_traffic_left(retort: Retort) -> None:
    left = Leftover(
        expire_at=NOW,
        days=Decimal(25),
        daily=Decimal(10),
        worth_days=Decimal("1.5"),
        traffic_left_gb=Decimal("5.04"),
        traffic_limit_gb=100,
    )
    m = manager(retort, left=left)
    m.container.deps[TranslatorRunner] = I18n()
    shown = await dialog.change_mode_getter(m)  # type: ignore[arg-type]
    assert (shown["s_traffic"], shown["s_traffic_left"], shown["s_traffic_limit"]) == (
        1,
        Decimal("5.0"),
        100,
    )
    assert shown["s_left_value"] == Decimal(15)


def test_the_dialog_takes_the_windows() -> None:
    states = {window.get_state() for window in subscription_dialog.windows.values()}
    assert {Subscription.S_CHANGE_MODE, Subscription.S_CHANGE_DEVICES} <= states


class Gateways:
    async def system(self, gateway_type: Any) -> Any:
        data = type("Data", (), {"type": gateway_type, "currency": RUB, "settings": None})
        return type("Gateway", (), {"data": data})()


class Hub:
    def get_translator_by_locale(self, locale: Any) -> I18n:
        return I18n()


class Pending:
    def __init__(self, existing: TransactionDto) -> None:
        self.existing = existing
        self.created: list[TransactionDto] = []

    async def get_recent_pending(self, **kwargs: Any) -> TransactionDto:
        return self.existing

    async def create(self, transaction: TransactionDto) -> None:
        self.created.append(transaction)


@pytest.mark.parametrize(
    "before, now, reused",
    [
        (None, None, True),
        (PlanChangeDto(drop_hwids=["h1"]), PlanChangeDto(drop_hwids=["h1"]), True),
        (PlanChangeDto(drop_hwids=["h1"]), PlanChangeDto(drop_hwids=["h2"]), False),
        (None, PlanChangeDto(mode=ChangeMode.KEEP_EXPIRY), False),
    ],
)
async def test_a_free_change_reuses_only_the_same_choice(
    before: Optional[PlanChangeDto], now: Optional[PlanChangeDto], reused: bool
) -> None:
    existing = paid("0", plan_id=2, change=before)
    existing.status = TransactionStatus.PENDING
    dao = Pending(existing)
    use_case = CreatePayment(Uow(), None, dao, Gateways(), Hub())  # type: ignore[arg-type]
    free = price("0")
    free.change = now
    result = await use_case._execute(
        USER,
        CreatePaymentDto(
            plan_snapshot=snapshot(2),
            pricing=free,
            purchase_type=PurchaseType.CHANGE,
            gateway_type=PaymentGatewayType.YOOKASSA,
        ),
    )
    assert (result.id == existing.payment_id) == reused
    assert len(dao.created) == (0 if reused else 1)
