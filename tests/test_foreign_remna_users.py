"""Panel users made by other bots for the same Telegram account.

One Telegram account can be in the panel several times, once per bot that
registered it. The bot used to take any panel user with a matching Telegram id:
the first one found was shown and compared in the user card, a "modified"
webhook of another bot's user overwrote the subscription here with that user's
link and expiry, and syncing a user without a subscription deleted another
bot's user from the panel. Only RemnaShop's own users (``rs_<telegram id>``, or
tagged ``IMPORTED``) are taken by Telegram id now, except by an import from the
panel the admin asked for, for someone with no subscription here. A panel user
already linked to a subscription is still followed by its id, whatever its name.
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from src.application.dto import UserDto
from src.application.use_cases.remnawave.commands.synchronization import (
    SyncRemnaUser,
    SyncRemnaUserDto,
)
from src.application.use_cases.subscription.commands.sync import SyncSubscriptionFromRemnashop
from src.core.enums import Role
from src.core.utils.remna_id import is_own_remna_user, own_remna_user

ADMIN = UserDto(id=1, telegram_id=1, name="Admin", role=Role.OWNER)
BUYER = UserDto(id=7, telegram_id=777, name="Buyer")


def panel_user(username: str, tag: Optional[str] = None, panel_id: int = 10) -> Any:
    return SimpleNamespace(id=panel_id, username=username, tag=tag, telegram_id=777)


OTHER_BOT = panel_user("other_777", panel_id=10)
OURS = panel_user("rs_777", panel_id=11)


@pytest.mark.parametrize(
    "user, own",
    [
        (OURS, True),
        (panel_user("rs_web_5"), True),
        (panel_user("vpn_777", tag="IMPORTED"), True),
        (OTHER_BOT, False),
        (panel_user("777"), False),
    ],
)
def test_whose_panel_user(user: Any, own: bool) -> None:
    assert is_own_remna_user(user) is own


def test_ours_is_taken_whatever_the_panel_lists_first() -> None:
    assert own_remna_user([OTHER_BOT, OURS]) is OURS
    assert own_remna_user([OTHER_BOT]) is None


class FakeUow:
    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class Users:
    def __init__(self, linked: Optional[UserDto] = None) -> None:
        self.linked = linked

    async def get_by_remna_uuid(self, uuid: Any) -> Optional[UserDto]:
        return self.linked

    async def get_by_telegram_id(self, telegram_id: int) -> UserDto:
        return BUYER

    async def get_by_id(self, user_id: int) -> UserDto:
        return BUYER


class Subscriptions:
    """Answers ``get_current`` with ``answers`` first; then the user was taken."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asked = False

    async def get_current(self, user_id: int) -> Any:
        if self.answers:
            return self.answers.pop(0)
        self.asked = True
        raise RuntimeError("stop here: the panel user was taken")


def sync(users: Users, subscriptions: Subscriptions) -> SyncRemnaUser:
    return SyncRemnaUser(FakeUow(), users, subscriptions, None, None, None)  # type: ignore[arg-type]


class TestWebhookSync:
    async def test_another_bots_user_does_not_touch_the_subscription(self) -> None:
        subscriptions = Subscriptions()
        taken = await sync(Users(), subscriptions).system(SyncRemnaUserDto(OTHER_BOT, False))
        assert taken is False
        assert not subscriptions.asked

    async def test_our_user_is_found_by_telegram_id(self) -> None:
        subscriptions = Subscriptions()
        with pytest.raises(RuntimeError, match="taken"):
            await sync(Users(), subscriptions).system(SyncRemnaUserDto(OURS, False))
        assert subscriptions.asked

    async def test_a_linked_user_is_followed_whatever_its_name(self) -> None:
        subscriptions = Subscriptions()
        with pytest.raises(RuntimeError, match="taken"):
            await sync(Users(linked=BUYER), subscriptions).system(
                SyncRemnaUserDto(OTHER_BOT, False)
            )
        assert subscriptions.asked


class TestImport:
    """Moving from another bot: the admin imports everyone from the panel."""

    async def test_another_bots_user_is_imported_for_one_without_a_subscription(
        self,
    ) -> None:
        subscriptions = Subscriptions(None)
        with pytest.raises(RuntimeError, match="taken"):
            await sync(Users(), subscriptions).system(SyncRemnaUserDto(OTHER_BOT, True))
        assert subscriptions.asked

    async def test_it_never_replaces_a_subscription_made_here(self) -> None:
        subscriptions = Subscriptions(object())
        taken = await sync(Users(), subscriptions).system(SyncRemnaUserDto(OTHER_BOT, True))
        assert taken is False
        assert not subscriptions.asked


class Panel:
    def __init__(self, *users: Any) -> None:
        self.users = list(users)
        self.deleted: list[Any] = []

    async def get_users_by_telegram_id(self, telegram_id: int) -> list[Any]:
        return self.users

    async def delete_user(self, uuid: Any) -> bool:
        self.deleted.append(uuid.int)
        return True


class NoSubscription:
    async def get_current(self, user_id: int) -> None:
        return None


async def push_to_panel(panel: Panel) -> list[Any]:
    use_case = SyncSubscriptionFromRemnashop(
        FakeUow(),  # type: ignore[arg-type]
        Users(),  # type: ignore[arg-type]
        NoSubscription(),  # type: ignore[arg-type]
        panel,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    await use_case(ADMIN, BUYER.id)
    return panel.deleted


class TestSyncToPanel:
    async def test_another_bots_user_is_not_deleted(self) -> None:
        assert await push_to_panel(Panel(OTHER_BOT)) == []

    async def test_only_ours_is_deleted(self) -> None:
        assert await push_to_panel(Panel(OTHER_BOT, OURS)) == [OURS.id]
