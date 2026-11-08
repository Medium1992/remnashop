"""Adding or taking days in the user card ("expiry time").

Taking more days than remain ends the subscription now rather than being
refused. Days used to be added to the old expiry date even when it had passed: a
subscription lapsed a week ago plus two days was still in the past and was
refused, and one lapsed half a day ago got a day and a half instead of two.
"""

from datetime import timedelta
from typing import Any, List

import pytest
from test_service_end_to_end import a_subscription

from src.application.dto import UserDto
from src.application.use_cases.subscription.commands.management import (
    AddSubscriptionDuration,
    AddSubscriptionDurationDto,
)
from src.core.enums import Role, SubscriptionStatus
from src.core.utils.time import datetime_now

ADMIN = UserDto(id=1, telegram_id=1, name="Admin", role=Role.OWNER)


class FakeUow:
    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class FakeUserDao:
    async def get_by_id(self, user_id: int) -> UserDto:
        return UserDto(id=user_id, telegram_id=123, name="Test")


class FakeSubscriptionDao:
    def __init__(self, subscription: Any) -> None:
        self.subscription = subscription

    async def get_current(self, user_id: int) -> Any:
        return self.subscription

    async def update(self, subscription: Any) -> None:
        self.subscription = subscription


class RecordingPanel:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    async def update_user(self, user: Any, uuid: Any, subscription: Any) -> None:
        self.sent.append((subscription.expire_at, subscription.status))


async def add(days: int, *, expired_days_ago: float = 0, expires_in_days: float = 0) -> Any:
    now = datetime_now()
    expire_at = now - timedelta(days=expired_days_ago) + timedelta(days=expires_in_days)
    status = SubscriptionStatus.EXPIRED if expire_at < now else SubscriptionStatus.ACTIVE
    dao = FakeSubscriptionDao(a_subscription(expire_at=expire_at, status=status))
    panel = RecordingPanel()
    use_case = AddSubscriptionDuration(FakeUow(), FakeUserDao(), dao, panel)  # type: ignore[arg-type]
    await use_case(ADMIN, AddSubscriptionDurationDto(user_id=7, days=days))
    return dao.subscription, panel, now


class TestLapsed:
    async def test_days_given_after_a_long_lapse_count_from_now(self) -> None:
        subscription, panel, now = await add(2, expired_days_ago=7)

        assert subscription.expire_at >= now + timedelta(days=2)
        assert subscription.expire_at < now + timedelta(days=2, minutes=1)
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert panel.sent

    async def test_a_recent_lapse_gets_the_full_days(self) -> None:
        # This one used to end up with a day and a half.
        subscription, _, now = await add(2, expired_days_ago=0.5)
        assert subscription.expire_at >= now + timedelta(days=2)

    async def test_days_cannot_be_taken_from_a_lapsed_one(self) -> None:
        with pytest.raises(ValueError):
            await add(-1, expired_days_ago=3)


class TestActive:
    async def test_days_are_added_to_the_current_date(self) -> None:
        subscription, _, now = await add(3, expires_in_days=10)
        assert subscription.expire_at >= now + timedelta(days=13) - timedelta(minutes=1)
        assert subscription.expire_at <= now + timedelta(days=13)

    async def test_days_can_be_taken_while_some_remain(self) -> None:
        subscription, _, now = await add(-2, expires_in_days=10)
        assert subscription.expire_at <= now + timedelta(days=8)

    async def test_taking_more_than_remains_ends_it_now(self) -> None:
        # 12 hours left, minus a day: the subscription ends, it is not refused.
        subscription, panel, now = await add(-1, expires_in_days=0.5)

        # The panel takes no past date; just ahead, and its expiry job does the rest.
        assert now < subscription.expire_at <= now + timedelta(minutes=1, seconds=5)
        assert panel.sent
