"""Referral days kept until the referrer takes them."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from sqlalchemy.dialects import postgresql

import src.infrastructure.services  # noqa: F401  # before the routers, as in the app
from src.application.dto import ReferralRewardDto, SettingsDto, UserDto
from src.application.use_cases.referral.commands.rewards import GiveReferrerRewardDto
from src.core.enums import ReferralRewardType, SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.database.dao.s_bonus import BonusDaysDao
from src.infrastructure.services.s.bonus import (
    BonusDays,
    BonusDaysError,
    BonusDaysKeptEvent,
    SGiveReferrerReward,
)

USER = UserDto(id=7, telegram_id=7, name="Referrer")
DAYS = ReferralRewardType.EXTRA_DAYS


def settings(enabled: bool = True, plan_id: Optional[int] = 3) -> SettingsDto:
    s = SettingsDto()
    s.extra.s.bonus.enabled = enabled
    s.extra.s.bonus.plan_id = plan_id
    return s


class Settings:
    def __init__(self, value: SettingsDto) -> None:
        self.value = value

    async def get(self) -> SettingsDto:
        return self.value


class FakeUow:
    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class Rewards:
    """``referral_rewards`` of one user: id -> (days, issued)."""

    def __init__(self, **rows: int) -> None:
        self.rows = {int(k[1:]): [v, False] for k, v in rows.items()}

    async def pending(self, user_id: int) -> int:
        return sum(days for days, issued in self.rows.values() if not issued)

    async def claim(self, user_id: int) -> dict[int, int]:
        taken = {i: row[0] for i, row in self.rows.items() if not row[1]}
        for i in taken:
            self.rows[i][1] = True
        return taken

    async def release(self, ids: list[int]) -> None:
        for i in ids:
            self.rows[i][1] = False


class Subscriptions:
    def __init__(self, subscription: Any = None) -> None:
        self.subscription = subscription

    async def get_current(self, user_id: int) -> Any:
        return self.subscription


class Plans:
    async def get_by_id(self, plan_id: int) -> Any:
        return SimpleNamespace(id=plan_id) if plan_id == 3 else None


class Recorder:
    def __init__(self, after: Any = None, fails: bool = False) -> None:
        self.calls: list[Any] = []
        self.after = after
        self.fails = fails
        self.subscriptions: Optional[Subscriptions] = None

    @property
    def system(self) -> "Recorder":
        return self

    async def __call__(self, data: Any) -> None:
        self.calls.append(data)
        if self.fails:
            raise RuntimeError("panel is down")
        if self.subscriptions is not None:
            self.subscriptions.subscription = self.after


def subscription(**kwargs: Any) -> Any:
    values = {
        "status": SubscriptionStatus.ACTIVE,
        "is_unlimited": False,
        "is_trial": False,
        "expire_at": datetime_now() + timedelta(days=10),
    }
    return SimpleNamespace(**{**values, **kwargs})


def bonus(
    rewards: Rewards,
    current: Any = None,
    add: Optional[Recorder] = None,
    make: Optional[Recorder] = None,
    s: Optional[SettingsDto] = None,
) -> tuple[BonusDays, Recorder, Recorder]:
    subscriptions = Subscriptions(current)
    add = add or Recorder(after=current)
    make = make or Recorder(after=subscription())
    add.subscriptions = make.subscriptions = subscriptions
    service = BonusDays(
        FakeUow(),  # type: ignore[arg-type]
        rewards,  # type: ignore[arg-type]
        Settings(s or settings()),  # type: ignore[arg-type]
        subscriptions,  # type: ignore[arg-type]
        Plans(),  # type: ignore[arg-type]
        add,  # type: ignore[arg-type]
        make,  # type: ignore[arg-type]
    )
    return service, add, make


class TestTake:
    async def test_all_the_days_kept_go_to_the_subscription(self) -> None:
        rewards = Rewards(r1=5, r2=7)
        service, add, make = bonus(rewards, current=subscription())
        taken = await service.take(USER)
        assert taken.days == 12
        assert [c.days for c in add.calls] == [12] and not make.calls
        assert await rewards.pending(USER.id) == 0

    @pytest.mark.parametrize("status", [SubscriptionStatus.EXPIRED, SubscriptionStatus.ACTIVE])
    async def test_a_lapsed_or_trial_subscription_gets_them_too(
        self, status: SubscriptionStatus
    ) -> None:
        service, add, _ = bonus(Rewards(r1=5), current=subscription(status=status, is_trial=True))
        await service.take(USER)
        assert [c.days for c in add.calls] == [5]

    async def test_without_a_subscription_one_is_made_on_the_chosen_plan(self) -> None:
        service, add, make = bonus(Rewards(r1=5, r2=2))
        await service.take(USER)
        assert not add.calls
        assert [(c.plan_id, c.duration) for c in make.calls] == [(3, 7)]

    async def test_a_deleted_subscription_counts_as_none(self) -> None:
        deleted = subscription(status=SubscriptionStatus.DELETED)
        service, add, make = bonus(Rewards(r1=5), current=deleted)
        await service.take(USER)
        assert not add.calls and len(make.calls) == 1

    @pytest.mark.parametrize(
        "current, s, reason",
        [
            (None, settings(plan_id=None), "no-plan"),
            (None, settings(plan_id=99), "no-plan"),
            (subscription(is_unlimited=True), settings(), "unlimited"),
        ],
    )
    async def test_the_days_stay_kept_when_they_cannot_go_anywhere(
        self, current: Any, s: SettingsDto, reason: str
    ) -> None:
        rewards = Rewards(r1=5)
        service, _, _ = bonus(rewards, current=current, s=s)
        with pytest.raises(BonusDaysError, match=reason):
            await service.take(USER)
        assert await rewards.pending(USER.id) == 5

    async def test_the_days_stay_kept_when_the_panel_fails(self) -> None:
        rewards = Rewards(r1=5)
        service, _, _ = bonus(rewards, current=subscription(), add=Recorder(fails=True))
        with pytest.raises(RuntimeError, match="panel"):
            await service.take(USER)
        assert await rewards.pending(USER.id) == 5

    async def test_nothing_to_take(self) -> None:
        service, add, make = bonus(Rewards())
        with pytest.raises(BonusDaysError, match="none"):
            await service.take(USER)
        assert not add.calls and not make.calls


class Events:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.sent.append(event)


class Users:
    async def get_by_id(self, user_id: int) -> UserDto:
        return USER


class Referrals:
    def __init__(self) -> None:
        self.issued: list[int] = []

    async def mark_reward_as_issued(self, reward_id: int) -> None:
        self.issued.append(reward_id)


def give(
    s: SettingsDto, rewards: Rewards
) -> tuple[SGiveReferrerReward, Recorder, Events, Referrals]:
    add, events, referrals = Recorder(), Events(), Referrals()
    use_case = SGiveReferrerReward(
        FakeUow(),  # type: ignore[arg-type]
        Users(),  # type: ignore[arg-type]
        Subscriptions(subscription()),  # type: ignore[arg-type]
        referrals,  # type: ignore[arg-type]
        events,
        None,  # type: ignore[arg-type]
        add,  # type: ignore[arg-type]
        Settings(s),  # type: ignore[arg-type]
        rewards,  # type: ignore[arg-type]
    )
    return use_case, add, events, referrals


def reward(amount: int = 5, kind: ReferralRewardType = DAYS) -> GiveReferrerRewardDto:
    return GiveReferrerRewardDto(
        user_id=USER.id,
        reward=ReferralRewardDto(id=11, user_id=USER.id, type=kind, amount=amount),
        referred_name="Friend",
    )


class TestReward:
    async def test_days_are_kept_not_added(self) -> None:
        # The new reward is in the table already, with an older one lost before.
        use_case, add, events, referrals = give(settings(), Rewards(r1=3, r11=5))
        await use_case.system(reward(5))
        assert not add.calls and not referrals.issued
        (event,) = events.sent
        assert isinstance(event, BonusDaysKeptEvent)
        assert (event.value, event.total, event.event_key) == (5, 8, "event-s-bonus.kept")

    async def test_off_the_days_are_added_as_upstream_does(self) -> None:
        use_case, add, events, referrals = give(settings(enabled=False), Rewards(r11=5))
        await use_case.system(reward(5))
        assert [c.days for c in add.calls] == [5]
        assert referrals.issued == [11]
        assert not isinstance(events.sent[0], BonusDaysKeptEvent)


def test_only_days_not_yet_issued_are_counted_and_claimed() -> None:
    dao = BonusDaysDao(None)  # type: ignore[arg-type]
    dialect: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
    where = " AND ".join(str(c.compile(dialect=dialect)) for c in dao._pending(7))
    assert "referral_rewards.user_id = %(user_id_1)s" in where
    assert "referral_rewards.type = %(type_1)s" in where
    assert "referral_rewards.is_issued IS false" in where
    assert "referral_rewards.amount > %(amount_1)s" in where
