"""One-time invite links: issued, spent once, expiring, and read by the
referral system through the real access and user middlewares."""

import json
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from aiogram import Router
from aiogram.types import Chat, Message
from aiogram.types import User as AiogramUser
from aiogram_dialog import setup_dialogs

from src.application.common import Notifier
from src.application.common.dao import SettingsDao, UserDao
from src.application.dto import SettingsDto, UserDto
from src.application.use_cases.access.queries.availability import CheckAccess, CheckAccessDto
from src.application.use_cases.referral.commands.attachment import (
    AttachReferral,
    AttachReferralDto,
)
from src.application.use_cases.referral.queries.code import ValidateReferralCode
from src.application.use_cases.s.settings import NUMBER_FIELDS, TOGGLE_FIELDS
from src.application.use_cases.user.commands.activity import TrackUserActivity
from src.application.use_cases.user.commands.registration import (
    GetOrCreateUser,
    GetOrCreateUserDto,
    UpdateUserProfile,
)
from src.core.constants import CONTAINER_KEY
from src.core.enums import AccessMode, MiddlewareEventType
from src.core.utils.s_invited import INVITED_BY_LINK
from src.infrastructure.services.s.invites import (
    TOKEN_LENGTH,
    InviteLink,
    InviteLinks,
    is_token,
)
from src.telegram.middlewares import setup_middlewares
from src.telegram.middlewares._codes import parse_referral_code
from src.telegram.middlewares.access import AccessMiddleware
from src.telegram.middlewares.s_invites import SInviteMiddleware, Stage
from src.telegram.middlewares.user import UserMiddleware
from src.telegram.routers.s.invites import invite_extra

BOT_URL = "https://t.me/test_bot?start="
HOUR = 3600.0


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeRedis:
    """Strings with a TTL, on the test's clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.data: dict[str, tuple[str, Optional[float]]] = {}

    def _alive(self, name: str) -> Optional[str]:
        item = self.data.get(name)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= self.clock():
            del self.data[name]
            return None
        return value

    async def get(self, name: str) -> Optional[str]:
        return self._alive(name)

    async def set(self, name: str, value: str, ex: Optional[int] = None) -> None:
        self.data[name] = (value, self.clock() + ex if ex else None)

    async def getdel(self, name: str) -> Optional[str]:
        value = self._alive(name)
        self.data.pop(name, None)
        return value

    async def delete(self, name: str) -> None:
        self.data.pop(name, None)


class FakeSettingsDao:
    def __init__(self, enabled: bool = True, mode: AccessMode = AccessMode.PUBLIC) -> None:
        self.settings = SettingsDto()
        self.settings.extra.s.invites.enabled = enabled
        self.settings.access.mode = mode

    async def get(self) -> SettingsDto:
        return self.settings


class FakeUserDao:
    def __init__(self, *users: UserDto) -> None:
        self.users = {u.id: u for u in users}

    async def get_by_id(self, user_id: int) -> Optional[UserDto]:
        return self.users.get(user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> Optional[UserDto]:
        return next((u for u in self.users.values() if u.telegram_id == telegram_id), None)


class FakeBotService:
    async def get_referral_url(self, referral_code: str) -> str:
        return f"{BOT_URL}ref_{referral_code}"


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def notify_user(self, user: Any, i18n_key: str = "", **kwargs: Any) -> None:
        self.sent.append((user.telegram_id, i18n_key))


OWNER = UserDto(id=1, telegram_id=1001, name="Owner", referral_code="Ab3dE9")
FRIEND_TG = 2002
OTHER_TG = 3003


def a_service(
    settings: Optional[FakeSettingsDao] = None, users: Optional[FakeUserDao] = None
) -> tuple[InviteLinks, Clock]:
    clock = Clock()
    service = InviteLinks(
        redis=FakeRedis(clock),  # type: ignore[arg-type]
        settings_dao=settings or FakeSettingsDao(),  # type: ignore[arg-type]
        user_dao=users or FakeUserDao(OWNER),  # type: ignore[arg-type]
        bot_service=FakeBotService(),  # type: ignore[arg-type]
    )
    service.clock = clock
    return service, clock


# ── the service ──────────────────────────────────────────────────────────────


class TestTokens:
    async def test_issued_tokens_are_upper_case_and_never_a_referral_code(self) -> None:
        service, _ = a_service(FakeSettingsDao())
        service.settings_dao.settings.extra.s.invites.max_active = 500  # type: ignore[attr-defined]
        for _ in range(300):
            link = await service.issue(OWNER)
            assert link is not None
            assert len(link.token) == TOKEN_LENGTH
            # The /start handlers match lower-case words anywhere in the text.
            assert link.token == link.token.upper()
            assert is_token(link.token)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ABCDEF123456", True),
            ("Ab3dE9", False),  # a permanent referral code
            ("abcdef123456", False),
            ("ABCDEF12345", False),
            ("ABCDEF1234567", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_token(self, raw: Optional[str], expected: bool) -> None:
        assert is_token(raw) is expected

    @pytest.mark.parametrize(
        ("left", "hours"), [(24 * HOUR, 24), (HOUR + 1, 2), (HOUR, 1), (59, 1), (0, 0), (-5, 0)]
    )
    def test_hours_left_round_up(self, left: float, hours: int) -> None:
        assert InviteLink(token="X", expires_at=100 + left).hours_left(100) == hours


class TestLifecycle:
    async def test_a_link_points_at_its_owner_until_used_once(self) -> None:
        service, _ = a_service()
        link = await service.issue(OWNER)
        assert link is not None
        assert await service.url(link) == f"{BOT_URL}ref_{link.token}"

        assert await service.owner(link.token) == OWNER
        assert await service.owner(link.token) == OWNER  # looking does not spend

        assert await service.spend(link.token) == OWNER
        assert await service.spend(link.token) is None
        assert await service.owner(link.token) is None
        assert await service.live(OWNER.id) == []

    async def test_a_link_dies_after_its_lifetime(self) -> None:
        service, clock = a_service()
        link = await service.issue(OWNER)
        assert link is not None

        clock.now += 24 * HOUR - 1
        assert await service.owner(link.token) == OWNER
        clock.now += 1
        assert await service.owner(link.token) is None
        assert await service.spend(link.token) is None
        assert await service.live(OWNER.id) == []

    async def test_lifetime_follows_the_setting(self) -> None:
        settings = FakeSettingsDao()
        settings.settings.extra.s.invites.ttl_hours = 2
        service, clock = a_service(settings)
        link = await service.issue(OWNER)
        assert link is not None
        clock.now += 2 * HOUR
        assert await service.owner(link.token) is None

    async def test_current_reuses_the_live_link_and_replaces_a_spent_one(self) -> None:
        service, _ = a_service()
        first = await service.current(OWNER)
        assert await service.current(OWNER) == first

        await service.spend(first.token)
        second = await service.current(OWNER)
        assert second.token != first.token
        assert await service.owner(second.token) == OWNER

    async def test_current_replaces_an_expired_link(self) -> None:
        service, clock = a_service()
        first = await service.current(OWNER)
        clock.now += 25 * HOUR
        assert (await service.current(OWNER)).token != first.token

    async def test_at_most_max_active_live_links(self) -> None:
        service, _ = a_service()  # max_active = 3
        links = [await service.issue(OWNER) for _ in range(3)]
        assert all(links)
        assert await service.issue(OWNER) is None
        assert (await service.current(OWNER)) == links[-1]  # the newest is shown

        await service.spend(links[0].token)  # type: ignore[union-attr]
        assert await service.issue(OWNER) is not None  # a place freed up
        assert len(await service.live(OWNER.id)) == 3

    async def test_older_links_stay_usable_after_a_new_one(self) -> None:
        service, _ = a_service()
        old = await service.issue(OWNER)
        new = await service.issue(OWNER)
        assert old and new
        assert await service.spend(old.token) == OWNER
        assert await service.spend(new.token) == OWNER

    async def test_an_unreadable_list_is_dropped_not_fatal(self) -> None:
        service, _ = a_service()
        await service.redis.set("s_invites:1", "{not json")
        assert await service.live(OWNER.id) == []
        assert await service.current(OWNER)

    async def test_the_owner_list_expires_with_its_last_link(self) -> None:
        service, clock = a_service()
        await service.issue(OWNER)
        redis: FakeRedis = service.redis  # type: ignore[assignment]
        [key] = [k for k in redis.data if k.startswith("s_invites")]
        stored = json.loads(redis.data[key][0])
        assert len(stored) == 1
        clock.now += 24 * HOUR
        assert await redis.get(key) is None

    async def test_a_deleted_owner_makes_the_link_worthless(self) -> None:
        service, _ = a_service(users=FakeUserDao(OWNER))
        link = await service.issue(OWNER)
        assert link is not None
        service.user_dao.users.clear()  # type: ignore[attr-defined]
        assert await service.owner(link.token) is None


class TestWhatIsShared:
    async def test_off_shares_the_permanent_link(self) -> None:
        service, _ = a_service(FakeSettingsDao(enabled=False))
        assert await service.referral_url(OWNER) == f"{BOT_URL}ref_{OWNER.referral_code}"
        assert service.redis.data == {}  # type: ignore[attr-defined]

    async def test_on_shares_one_time_links_and_keeps_the_same_until_used(self) -> None:
        service, _ = a_service()
        url = await service.referral_url(OWNER)
        assert url != f"{BOT_URL}ref_{OWNER.referral_code}"
        assert is_token(url.removeprefix(f"{BOT_URL}ref_"))
        # Copy, QR and the inline share all show this one link.
        assert await service.referral_url(OWNER) == url


# ── through the real middlewares ─────────────────────────────────────────────


class FakeValidateReferralCode:
    def __init__(self, users: FakeUserDao) -> None:
        self.users = users
        self.seen: list[str] = []

    async def system(self, data: Any) -> bool:
        self.seen.append(data.referral_code)
        return any(u.referral_code == data.referral_code for u in self.users.users.values())


class FakeCheckAccess:
    """The decision upstream makes for a new user, by the access mode."""

    def __init__(self, settings: FakeSettingsDao) -> None:
        self.settings = settings
        self.seen: list[CheckAccessDto] = []

    async def system(self, data: CheckAccessDto) -> bool:
        self.seen.append(data)
        if self.settings.settings.access.mode == AccessMode.INVITED:
            return data.is_referral_event
        return True


class FakeGetOrCreateUser:
    def __init__(self, users: FakeUserDao) -> None:
        self.users = users
        self.seen: list[Optional[str]] = []
        self.by_link: list[bool] = []

    async def system(self, data: GetOrCreateUserDto) -> Optional[UserDto]:
        self.seen.append(data.referral_code)
        self.by_link.append(INVITED_BY_LINK.get())
        existing = await self.users.get_by_telegram_id(data.telegram_id)
        if existing:
            return existing
        new = UserDto(id=len(self.users.users) + 1, telegram_id=data.telegram_id, name="New")
        self.users.users[new.id] = new
        return None  # the profile/activity steps of a new user are not under test here


class _Passthrough:
    async def system(self, data: Any) -> Any:
        return getattr(data, "user", None)


class World:
    def __init__(self, enabled: bool = True, mode: AccessMode = AccessMode.PUBLIC) -> None:
        self.settings = FakeSettingsDao(enabled=enabled, mode=mode)
        self.users = FakeUserDao(OWNER)
        self.invites, self.clock = a_service(self.settings, self.users)
        self.notifier = FakeNotifier()
        self.validate = FakeValidateReferralCode(self.users)
        self.access = FakeCheckAccess(self.settings)
        self.registration = FakeGetOrCreateUser(self.users)
        self.handled: list[int] = []

    async def get(self, kind: Any) -> Any:
        return {
            InviteLinks: self.invites,
            UserDao: self.users,
            SettingsDao: self.settings,
            Notifier: self.notifier,
            ValidateReferralCode: self.validate,
            CheckAccess: self.access,
            GetOrCreateUser: self.registration,
            UpdateUserProfile: _Passthrough(),
            TrackUserActivity: _Passthrough(),
        }[kind]

    async def start(self, telegram_id: int, text: str) -> bool:
        """Send a message through the middlewares in their real order."""
        user = AiogramUser(id=telegram_id, is_bot=False, first_name="T")
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=telegram_id, type="private"),
            from_user=user,
            text=text,
        )
        chain = [
            SInviteMiddleware(Stage.PEEK),
            AccessMiddleware(),
            SInviteMiddleware(Stage.SPEND),
            UserMiddleware(),
        ]
        reached: list[bool] = []

        async def handler(event: Any, data: dict[str, Any]) -> None:
            reached.append(True)
            self.handled.append(telegram_id)

        def link(i: int) -> Any:
            if i == len(chain):
                return handler

            async def call(event: Any, data: dict[str, Any]) -> Any:
                return await chain[i](link(i + 1), event, data)

            return call

        await link(0)(message, {CONTAINER_KEY: self, "event_from_user": user})
        return bool(reached)


class TestThroughTheMiddlewares:
    async def test_off_leaves_permanent_links_alone(self) -> None:
        world = World(enabled=False)
        assert await world.start(FRIEND_TG, f"/start ref_{OWNER.referral_code}")
        assert world.registration.seen == [OWNER.referral_code]
        assert world.notifier.sent == []

    async def test_a_live_link_registers_a_referral_and_is_spent(self) -> None:
        world = World()
        link = await world.invites.current(OWNER)

        assert await world.start(FRIEND_TG, f"/start ref_{link.token}")

        # Upstream saw the owner's ordinary referral code everywhere.
        assert world.validate.seen == [OWNER.referral_code]
        assert world.access.seen[0].is_referral_event is True
        assert world.registration.seen == [OWNER.referral_code]
        assert await world.invites.owner(link.token) is None
        assert world.notifier.sent == []

    async def test_the_second_person_on_a_used_link_gets_no_referral(self) -> None:
        world = World()
        link = await world.invites.current(OWNER)
        await world.start(FRIEND_TG, f"/start ref_{link.token}")

        assert await world.start(OTHER_TG, f"/start ref_{link.token}")
        assert world.registration.seen == [OWNER.referral_code, None]
        assert world.notifier.sent == [(OTHER_TG, "ntf-s-invites.dead")]

    async def test_an_expired_link_gives_no_referral(self) -> None:
        world = World()
        link = await world.invites.current(OWNER)
        world.clock.now += 25 * HOUR
        assert await world.start(FRIEND_TG, f"/start ref_{link.token}")
        assert world.registration.seen == [None]
        assert world.notifier.sent == [(FRIEND_TG, "ntf-s-invites.dead")]

    async def test_the_permanent_link_stops_bringing_referrals(self) -> None:
        world = World()
        assert await world.start(FRIEND_TG, f"/start ref_{OWNER.referral_code}")
        assert world.registration.seen == [None]
        assert world.validate.seen == []  # never even looked up
        assert world.notifier.sent == [(FRIEND_TG, "ntf-s-invites.dead")]

    async def test_an_existing_user_does_not_burn_a_friends_link(self) -> None:
        world = World()
        link = await world.invites.current(OWNER)
        assert await world.start(OWNER.telegram_id, f"/start ref_{link.token}")  # type: ignore[arg-type]
        assert await world.invites.owner(link.token) == OWNER
        assert world.notifier.sent == []

        assert await world.start(FRIEND_TG, f"/start ref_{link.token}")
        assert world.registration.seen[-1] == OWNER.referral_code

    async def test_an_existing_user_on_a_dead_link_is_not_told_off(self) -> None:
        world = World()
        assert await world.start(OWNER.telegram_id, "/start ref_ZZZZZZZZZZZZ")  # type: ignore[arg-type]
        assert world.notifier.sent == []

    async def test_invite_only_lets_a_live_link_in(self) -> None:
        world = World(mode=AccessMode.INVITED)
        link = await world.invites.current(OWNER)
        assert await world.start(FRIEND_TG, f"/start ref_{link.token}")
        assert world.registration.seen == [OWNER.referral_code]

    async def test_invite_only_keeps_a_used_link_out(self) -> None:
        world = World(mode=AccessMode.INVITED)
        link = await world.invites.current(OWNER)
        await world.start(FRIEND_TG, f"/start ref_{link.token}")

        assert not await world.start(OTHER_TG, f"/start ref_{link.token}")
        assert world.access.seen[-1].is_referral_event is False
        assert world.registration.seen == [OWNER.referral_code]

    async def test_invite_only_keeps_the_permanent_link_out(self) -> None:
        world = World(mode=AccessMode.INVITED)
        assert not await world.start(FRIEND_TG, f"/start ref_{OWNER.referral_code}")

    async def test_a_lost_race_on_one_link(self) -> None:
        """Two people open one link at once: both pass the access check, the
        first registration spends it, the second gets no referral."""
        world = World()
        link = await world.invites.current(OWNER)
        await world.invites.spend(link.token)  # the winner, between peek and spend

        world.invites.owner = _alive(OWNER)  # type: ignore[method-assign]
        assert await world.start(OTHER_TG, f"/start ref_{link.token}")
        assert world.registration.seen == [None]
        assert world.notifier.sent == [(OTHER_TG, "ntf-s-invites.dead")]

    async def test_a_lost_race_in_invite_only_mode_stops_the_loser(self) -> None:
        world = World(mode=AccessMode.INVITED)
        link = await world.invites.current(OWNER)
        await world.invites.spend(link.token)
        world.invites.owner = _alive(OWNER)  # type: ignore[method-assign]

        assert not await world.start(OTHER_TG, f"/start ref_{link.token}")
        assert world.registration.seen == []
        assert world.notifier.sent == [(OTHER_TG, "ntf-s-invites.dead")]

    @pytest.mark.parametrize("text", ["/start", "hello", "/start plan_ABC", "/start promo_X"])
    async def test_other_messages_pass_untouched(self, text: str) -> None:
        world = World()
        assert await world.start(FRIEND_TG, text)
        assert world.notifier.sent == []
        assert world.registration.seen == [None]
        assert world.invites.redis.data == {}  # type: ignore[attr-defined]


def _alive(owner: UserDto) -> Any:
    async def owner_of(token: str) -> UserDto:
        return owner

    return owner_of


class TestHooks:
    def test_the_override_replaces_the_code_in_start(self) -> None:
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            text="/start ref_ABCDEF123456",
        )
        assert parse_referral_code(message) == "ABCDEF123456"
        assert parse_referral_code(message, {}) == "ABCDEF123456"
        assert parse_referral_code(message, {"s_referral_code": "Ab3dE9"}) == "Ab3dE9"
        assert parse_referral_code(message, {"s_referral_code": ""}) is None

    def test_middleware_order(self) -> None:
        router = Router()
        setup_dialogs(router)  # adds the aiogram-dialog update type, as in production
        setup_middlewares(router)
        chain = [type(m) for m in router.observers[MiddlewareEventType.MESSAGE].outer_middleware]
        stages = [
            m.stage
            for m in router.observers[MiddlewareEventType.MESSAGE].outer_middleware
            if isinstance(m, SInviteMiddleware)
        ]
        assert stages == [Stage.PEEK, Stage.SPEND]
        peek, spend = (i for i, t in enumerate(chain) if t is SInviteMiddleware)
        assert peek < chain.index(AccessMiddleware) < spend < chain.index(UserMiddleware)

    def test_settings_are_editable_within_bounds(self) -> None:
        assert NUMBER_FIELDS["invites.ttl_hours"] == (1, 168)
        assert NUMBER_FIELDS["invites.max_active"] == (1, 20)
        assert "invites.enabled" in TOGGLE_FIELDS

    def test_off_by_default(self) -> None:
        invites = SettingsDto().extra.s.invites
        assert (invites.enabled, invites.ttl_hours, invites.max_active) == (False, 24, 3)


# ── the fixes after the first days in use ────────────────────────────────────


class TestRegisteredThroughALink:
    """A spent link records the referral even with the referral system off."""

    async def test_a_spent_link_marks_the_registration(self) -> None:
        world = World()
        link = await world.invites.current(OWNER)
        await world.start(FRIEND_TG, f"/start ref_{link.token}")
        assert world.registration.by_link == [True]
        assert INVITED_BY_LINK.get() is False  # reset after the update

    async def test_the_permanent_link_does_not(self) -> None:
        world = World(enabled=False)
        await world.start(FRIEND_TG, f"/start ref_{OWNER.referral_code}")
        assert world.registration.by_link == [False]

    async def test_an_existing_user_does_not(self) -> None:
        world = World()
        world.users.users[7] = UserDto(id=7, telegram_id=FRIEND_TG, name="Old")
        link = await world.invites.current(OWNER)
        await world.start(FRIEND_TG, f"/start ref_{link.token}")
        assert world.registration.by_link == [False]


class _Uow:
    async def __aenter__(self) -> "_Uow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def commit(self) -> None:
        pass


class _ReferralDao:
    def __init__(self) -> None:
        self.created: list[Any] = []

    async def get_referral_chain(self, user_id: int) -> tuple[None, None]:
        return None, None

    async def create_referral(self, referral: Any) -> None:
        self.created.append(referral)


class _Users(FakeUserDao):
    async def get_by_referral_code(self, code: str) -> Optional[UserDto]:
        return next((u for u in self.users.values() if u.referral_code == code), None)


class _Events:
    async def publish(self, event: Any) -> None:
        pass


async def attach(referral_enabled: bool, by_link: bool) -> int:
    settings = FakeSettingsDao()
    settings.settings.referral.enable = referral_enabled
    friend = UserDto(id=2, telegram_id=FRIEND_TG, name="Friend")
    referrals = _ReferralDao()
    use_case = AttachReferral(
        _Uow(),  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        _Users(OWNER, friend),  # type: ignore[arg-type]
        referrals,  # type: ignore[arg-type]
        _Events(),
    )
    marker = INVITED_BY_LINK.set(by_link)
    try:
        await use_case.system(AttachReferralDto(user_id=friend.id, referral_code="Ab3dE9"))
    finally:
        INVITED_BY_LINK.reset(marker)
    return len(referrals.created)


class TestAttachWithReferralsOff:
    async def test_a_one_time_link_is_recorded(self) -> None:
        assert await attach(referral_enabled=False, by_link=True) == 1

    async def test_otherwise_upstream_skips_it(self) -> None:
        assert await attach(referral_enabled=False, by_link=False) == 0

    async def test_with_referrals_on_nothing_changes(self) -> None:
        assert await attach(referral_enabled=True, by_link=False) == 1


class TestRevoke:
    async def test_revoking_kills_every_live_link(self) -> None:
        invites, _ = a_service()
        first = await invites.current(OWNER)
        second = await invites.issue(OWNER)
        assert second is not None

        assert await invites.revoke_all(OWNER.id) == 2

        assert await invites.owner(first.token) is None
        assert await invites.owner(second.token) is None
        assert await invites.live(OWNER.id) == []

    async def test_the_next_link_follows_the_current_lifetime(self) -> None:
        settings = FakeSettingsDao()
        settings.settings.extra.s.invites.ttl_hours = 1
        invites, clock = a_service(settings)
        await invites.current(OWNER)
        settings.settings.extra.s.invites.ttl_hours = 24

        await invites.revoke_all(OWNER.id)
        link = await invites.current(OWNER)

        assert link.hours_left(clock()) == 24


class _I18n:
    def get(self, key: str, **kwargs: Any) -> str:
        return f"{kwargs['n']}|{kwargs['url']}|{kwargs['hours']}"


class TestTheInviteScreen:
    async def test_every_live_link_is_listed_newest_first(self) -> None:
        settings = FakeSettingsDao()
        invites, clock = a_service(settings)
        old = await invites.current(OWNER)
        clock.now += HOUR
        new = await invites.issue(OWNER)
        assert new is not None

        extra = await invite_extra(invites, OWNER, _I18n())  # type: ignore[arg-type]

        assert extra["s_invite_links"].splitlines() == [
            f"1|{BOT_URL}ref_{new.token}|24",
            f"2|{BOT_URL}ref_{old.token}|23",
        ]
        assert (extra["s_invite_active"], extra["s_invite_can_add"]) == (2, 1)

    async def test_off_shows_nothing(self) -> None:
        invites, _ = a_service(FakeSettingsDao(enabled=False))
        assert await invite_extra(invites, OWNER, _I18n()) == {"s_invite": 0}  # type: ignore[arg-type]
