"""The admin's user card: who invited the user."""

from types import SimpleNamespace
from typing import Any, Optional

import src.infrastructure.services  # noqa: F401  # before the routers, as in the app
from src.application.common.dao import ReferralDao
from src.core.constants import TARGET_USER_ID
from src.telegram.routers.s import user_card
from tests.test_s_receipts_admin import Manager


class Referrals:
    def __init__(self, referrer_id: Optional[int]) -> None:
        self.referrer_id = referrer_id

    async def get_by_referred_id(self, referred_id: int) -> Any:
        if self.referrer_id is None:
            return None
        return SimpleNamespace(referrer=SimpleNamespace(id=self.referrer_id))


def manager(referrer_id: Optional[int]) -> Manager:
    found = Manager({ReferralDao: Referrals(referrer_id)})
    found.dialog_data[TARGET_USER_ID] = 7
    return found


async def test_the_button_shows_only_for_an_invited_user() -> None:
    async def show(referrer_id: Optional[int]) -> Any:
        # As aiogram-dialog calls a getter: the middleware data as keywords.
        found = manager(referrer_id)
        return await user_card.inviter_getter(  # type: ignore[call-arg]
            dialog_manager=found, **found.middleware_data
        )

    shown, hidden = await show(3), await show(None)
    assert (shown, hidden) == ({"s_inviter": 1}, {"s_inviter": 0})


async def test_the_inviters_card_opens(monkeypatch: Any) -> None:
    opened: list[int] = []

    async def start_user_window(manager: Any, target_user_id: int, **kw: Any) -> None:
        opened.append(target_user_id)

    monkeypatch.setattr(user_card, "start_user_window", start_user_window)
    await user_card.on_open_inviter(None, None, manager(3))  # type: ignore[arg-type]
    await user_card.on_open_inviter(None, None, manager(None))  # type: ignore[arg-type]
    assert opened == [3]
