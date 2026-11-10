"""Forged ``gt_`` callbacks: callback data is whatever the client sends, so the
dashboard must not open for a user who only crafted the button."""

from typing import Any, Optional

import pytest
from aiogram.fsm.state import State

from src.application.dto import TelegramUserDto
from src.core.constants import GOTO_PREFIX
from src.core.enums import Role
from src.telegram.routers.extra.goto import on_goto
from src.telegram.states import (
    Dashboard,
    DashboardRemnashop,
    DashboardUser,
    MainMenu,
    Subscription,
)


class _Bg:
    def __init__(self, started: list[State]) -> None:
        self.started = started

    async def start(self, state: State, **kwargs: Any) -> None:
        self.started.append(state)


class _DialogManager:
    def __init__(self) -> None:
        self.started: list[State] = []

    def bg(self, **kwargs: Any) -> _Bg:
        return _Bg(self.started)


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = False

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answered = True


def _user(role: Role) -> TelegramUserDto:
    return TelegramUserDto(id=7, telegram_id=123, name="Test", role=role)


async def _goto(data: str, role: Role) -> tuple[Optional[State], _Callback]:
    manager = _DialogManager()
    callback = _Callback(f"{GOTO_PREFIX}{data}")
    await on_goto(callback, manager, _user(role))  # type: ignore[arg-type]
    return (manager.started[0] if manager.started else None), callback


@pytest.mark.parametrize(
    "data",
    [
        Dashboard.MAIN.state,
        DashboardRemnashop.MAIN.state,
        f"{DashboardUser.MAIN.state}:1",
    ],
)
async def test_a_user_can_not_open_the_dashboard_by_a_forged_button(data: str) -> None:
    started, callback = await _goto(data, Role.USER)

    assert started is None
    assert callback.answered


@pytest.mark.parametrize(
    "state", [MainMenu.MAIN, MainMenu.DEVICES, MainMenu.INVITE, Subscription.MAIN]
)
async def test_a_user_still_follows_the_buttons_sent_to_users(state: State) -> None:
    started, _ = await _goto(state.state, Role.USER)  # type: ignore[arg-type]

    assert started == state


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OWNER, Role.PREVIEW])
async def test_the_dashboard_opens_for_roles_that_see_it(role: Role) -> None:
    started, _ = await _goto(f"{DashboardUser.MAIN.state}:1", role)

    assert started == DashboardUser.MAIN
