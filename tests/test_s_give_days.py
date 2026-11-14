"""Giving a plan for any number of days from the user card, and taking bonus days."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from dishka import Provider, Scope, make_async_container
from dishka.integrations.aiogram import CONTAINER_NAME

import src.infrastructure.services  # noqa: F401  # before the routers, as in the app
from src.application.common import Notifier
from src.application.dto import UserDto
from src.application.use_cases.subscription.commands.set_plan import SetUserSubscription
from src.core.constants import TARGET_USER_ID, USER_KEY
from src.infrastructure.services.s.bonus import BonusDays, BonusDaysError, BonusDaysTaken
from src.telegram.routers.s.bonus import on_take_bonus
from src.telegram.routers.s.give_days import on_give_days_input
from src.telegram.states import DashboardUser

ADMIN = UserDto(id=1, telegram_id=1, name="Admin")


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def notify_user(self, user: Any, payload: Any = None, i18n_key: str = "") -> None:
        self.sent.append(payload.i18n_key if payload else i18n_key)
        if payload:
            self.sent.append(payload.i18n_kwargs)


class FakeSetUserSubscription:
    def __init__(self) -> None:
        self.given: list[Any] = []

    async def __call__(self, actor: Any, data: Any) -> None:
        self.given.append((data.user_id, data.plan_id, data.duration))


class FakeBonus:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def take(self, user: Any) -> BonusDaysTaken:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome  # type: ignore[no-any-return]


class Manager:
    def __init__(self, **deps: Any) -> None:
        provider = Provider(scope=Scope.APP)
        for kind in deps:
            provider.from_context(provides=TYPES[kind], scope=Scope.APP)
        context = {TYPES[kind]: value for kind, value in deps.items()}
        self.middleware_data = {
            CONTAINER_NAME: make_async_container(provider, context=context),
            USER_KEY: ADMIN,
        }
        self.dialog_data = {TARGET_USER_ID: 7, "selected_plan_id": 3}
        self.show_mode: Any = None
        self.state: Any = None

    async def switch_to(self, state: Any, **kwargs: Any) -> None:
        self.state = state


TYPES = {"notifier": Notifier, "give": SetUserSubscription, "bonus": BonusDays}


async def type_days(text: str) -> tuple[Manager, FakeNotifier, FakeSetUserSubscription]:
    notifier, give = FakeNotifier(), FakeSetUserSubscription()
    manager = Manager(notifier=notifier, give=give)
    message = SimpleNamespace(text=text)
    await on_give_days_input(message, None, manager)  # type: ignore[arg-type]
    return manager, notifier, give


async def test_the_plan_is_given_for_the_days_typed() -> None:
    manager, notifier, give = await type_days("5")
    assert give.given == [(7, 3, 5)]
    assert manager.state == DashboardUser.MAIN and not notifier.sent


@pytest.mark.parametrize("text", ["0", "-3", "abc", "", "3651"])
async def test_a_wrong_number_gives_nothing(text: str) -> None:
    manager, notifier, give = await type_days(text)
    assert not give.given and manager.state is None
    assert notifier.sent == ["ntf-s-give.days-invalid"]


async def press_take(outcome: Any) -> list[Any]:
    notifier = FakeNotifier()
    manager = Manager(notifier=notifier, bonus=FakeBonus(outcome))
    await on_take_bonus(None, None, manager)  # type: ignore[arg-type]
    return notifier.sent


async def test_taken_days_are_told_with_the_new_date() -> None:
    until = datetime(2026, 12, 1, tzinfo=timezone.utc)
    sent = await press_take(BonusDaysTaken(days=12, expire_at=until))
    assert sent[0] == "ntf-s-bonus.taken" and sent[1]["days"] == 12


@pytest.mark.parametrize("reason", ["none", "unlimited", "no-plan"])
async def test_why_the_days_were_not_taken(reason: str) -> None:
    assert await press_take(BonusDaysError(reason)) == [f"ntf-s-bonus.{reason}"]


async def test_a_failure_is_told_not_raised() -> None:
    assert await press_take(RuntimeError("panel is down")) == ["ntf-s-bonus.failed"]
