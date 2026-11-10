"""Calendar months: a plan named "1 month" ends on the same day next month."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List

import httpx
import pytest
from conftest import PanelStub, user_payload
from test_s_slots import FakeRedis, FakeSettingsDao, a_client
from test_service_end_to_end import a_plan, a_subscription, a_user

from src.application.use_cases.subscription.commands.purchase import (
    PurchaseSubscription,
    PurchaseSubscriptionDto,
)
from src.core.enums import PurchaseType
from src.core.utils.s_calendar import add_months, add_term

UTC = timezone.utc


def at(year: int, month: int, day: int, hour: int = 15, minute: int = 30) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestArithmetic:
    @pytest.mark.parametrize(
        "start, months, end",
        [
            (at(2026, 3, 10), 1, at(2026, 4, 10)),
            (at(2027, 1, 31), 1, at(2027, 2, 28)),  # no 31st: the month's last day
            (at(2028, 1, 31), 1, at(2028, 2, 29)),  # a leap year
            (at(2026, 8, 31), 1, at(2026, 9, 30)),
            (at(2026, 12, 15), 1, at(2027, 1, 15)),  # across the year
            (at(2026, 11, 30), 3, at(2027, 2, 28)),
            (at(2028, 2, 29), 12, at(2029, 2, 28)),
        ],
    )
    def test_add_months(self, start: datetime, months: int, end: datetime) -> None:
        assert add_months(start, months) == end

    def test_the_time_of_day_and_zone_stay(self) -> None:
        end = add_months(at(2026, 1, 31, 23, 59), 1)
        assert (end.hour, end.minute, end.tzinfo) == (23, 59, UTC)

    @pytest.mark.parametrize(
        "days, end",
        [
            (30, at(2026, 2, 28)),
            (60, at(2026, 3, 31)),
            (90, at(2026, 4, 30)),
            (180, at(2026, 7, 31)),
            (360, at(2027, 1, 31)),  # named "12 months"
            (365, at(2027, 1, 31)),
            (730, at(2028, 1, 31)),
        ],
    )
    def test_terms_named_in_months_and_years(self, days: int, end: datetime) -> None:
        assert add_term(at(2026, 1, 31), days) == end

    @pytest.mark.parametrize("days", [1, 3, 7, 14, 45])
    def test_other_terms_stay_in_days(self, days: int) -> None:
        start = at(2026, 1, 31)
        assert add_term(start, days) == start + timedelta(days=days)


# On the 31st a calendar month and 30 days part ways, whatever today is.
NOW = at(2027, 1, 31)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (
        "src.infrastructure.services.s.remnawave",
        "src.core.utils.converters",
        "src.application.use_cases.subscription.commands.purchase",
    ):
        monkeypatch.setattr(f"{module}.datetime_now", lambda: NOW)


def calendar(on: bool) -> FakeSettingsDao:
    settings = FakeSettingsDao(enabled=False)
    settings.settings.extra.s.plans.calendar_months = on
    return settings


def expiry_sent(panel: PanelStub, method: str) -> datetime:
    body = next(c.body for c in panel.calls if c.method == method and c.body)
    return datetime.fromisoformat(body["expireAt"].replace("Z", "+00:00"))


class TestPanelWrites:
    """A plan written to the panel: a new purchase, a plan change."""

    async def test_a_plan_change_ends_a_calendar_month_later(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        client = await a_client(sdk, http, panel, calendar(True), FakeRedis())

        await client.update_user(a_user(), a_subscription().user_remna_id, plan=a_plan())

        assert expiry_sent(panel, "PATCH") == at(2027, 2, 28)

    async def test_a_new_user_ends_a_calendar_month_later(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        client = await a_client(sdk, http, panel, calendar(True), FakeRedis())
        panel.on("POST", "/api/users", user_payload(line="v3"))

        await client.create_user(a_user(), plan=a_plan(duration=90))

        assert expiry_sent(panel, "POST") == at(2027, 4, 30)

    async def test_switched_off_a_month_is_thirty_days(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        client = await a_client(sdk, http, panel, calendar(False), FakeRedis())

        await client.update_user(a_user(), a_subscription().user_remna_id, plan=a_plan())

        assert expiry_sent(panel, "PATCH") == at(2027, 3, 2)

    async def test_a_subscription_keeps_its_own_expiry(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        client = await a_client(sdk, http, panel, calendar(True), FakeRedis())
        expire_at = NOW + timedelta(days=12)

        await client.update_user(
            a_user(),
            a_subscription().user_remna_id,
            subscription=a_subscription(expire_at=expire_at),
        )

        assert expiry_sent(panel, "PATCH") == expire_at


class FakeUow:
    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def commit(self) -> None:
        pass


class RecordingPanel:
    def __init__(self) -> None:
        self.written: List[Any] = []

    async def update_user(self, **kwargs: Any) -> None:
        self.written.append(kwargs["subscription"])


class FakeSubscriptionDao:
    async def update(self, subscription: Any) -> None:
        pass


class FakeUserDao:
    async def update(self, user: Any) -> None:
        pass


async def renew(subscription_expiry: datetime, duration: int, on: bool) -> datetime:
    panel = RecordingPanel()
    purchase = PurchaseSubscription(
        FakeUow(),  # type: ignore[arg-type]
        FakeUserDao(),  # type: ignore[arg-type]
        FakeSubscriptionDao(),  # type: ignore[arg-type]
        panel,  # type: ignore[arg-type]
        calendar(on),  # type: ignore[arg-type]
    )
    plan = a_plan(duration=duration)
    transaction = SimpleNamespace(id=1, plan_snapshot=plan, purchase_type=PurchaseType.RENEW)
    subscription = a_subscription(expire_at=subscription_expiry)
    await purchase.system(
        PurchaseSubscriptionDto(
            user=a_user(),
            transaction=transaction,  # type: ignore[arg-type]
            subscription=subscription,
        )
    )
    expire_at: datetime = panel.written[0].expire_at
    return expire_at


class TestRenewal:
    async def test_a_renewal_adds_a_calendar_month_to_the_expiry(self) -> None:
        assert await renew(at(2027, 3, 1), 30, on=True) == at(2027, 4, 1)

    async def test_a_lapsed_one_counts_from_now(self) -> None:
        assert await renew(NOW - timedelta(days=10), 30, on=True) == at(2027, 2, 28)

    async def test_switched_off_it_adds_thirty_days(self) -> None:
        assert await renew(at(2027, 3, 1), 30, on=False) == at(2027, 3, 31)

    @pytest.mark.parametrize("days", [7, 0])
    async def test_days_and_unlimited_are_left_as_they_were(self, days: int) -> None:
        expiry = at(2027, 3, 1)
        assert await renew(expiry, days, on=True) == await renew(expiry, days, on=False)
