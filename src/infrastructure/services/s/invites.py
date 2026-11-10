"""One-time invite links (docs/s/README.md, "Одноразовые приглашения").

A link is ``?start=ref_<TOKEN>``: the same deeplink as the permanent
``ref_<code>``, so the referral system — attachment, levels, rewards, the
invite-only access mode — works on it unchanged. What differs is how the code
is read: the token is resolved to its owner's referral code before the
upstream middlewares look at it, and it is spent on the first registration.

Tokens live in Redis only: the key's TTL is the link's lifetime and ``GETDEL``
spends it, so two people racing on one link cannot both win. Losing Redis
loses at most a day of unused links.
"""

import json
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Callable, Final, Optional

from loguru import logger
from redis.asyncio import Redis

from src.application.common import BotService
from src.application.common.dao import SettingsDao, UserDao
from src.application.dto import UserDto
from src.application.dto.s_settings import InviteLinksSettingsDto
from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import InviteOwnerKey, InviteTokenKey

# Upper case and digits only: the deeplink handlers match lower-case words
# ("plan", "promo", "invite") anywhere in /start, and a token must never trip
# them. Twelve characters also keep tokens apart from the six-character
# permanent referral codes.
TOKEN_ALPHABET: Final[str] = string.ascii_uppercase + string.digits
TOKEN_LENGTH: Final[int] = 12
TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^[A-Z0-9]{{{TOKEN_LENGTH}}}$")


def is_token(raw: Optional[str]) -> bool:
    return bool(raw) and bool(TOKEN_PATTERN.match(raw or ""))


def _token_key(token: str) -> str:
    return serialize_storage_key(InviteTokenKey(token=token))


def _owner_key(user_id: int) -> str:
    return serialize_storage_key(InviteOwnerKey(user_id=user_id))


@dataclass(frozen=True)
class InviteLink:
    token: str
    expires_at: float

    def hours_left(self, now: float) -> int:
        """Whole hours left, rounded up: "1" until the very last minute."""
        return max(0, -(-int(self.expires_at - now) // 3600))


class InviteLinks:
    def __init__(
        self,
        redis: Redis,
        settings_dao: SettingsDao,
        user_dao: UserDao,
        bot_service: BotService,
    ) -> None:
        self.redis = redis
        self.settings_dao = settings_dao
        self.user_dao = user_dao
        self.bot_service = bot_service
        self.clock: Callable[[], float] = time.time  # tests move it

    async def settings(self) -> InviteLinksSettingsDto:
        return (await self.settings_dao.get()).extra.s.invites

    async def live(self, user_id: int) -> list[InviteLink]:
        """The user's links still usable, oldest first; prunes the rest."""
        raw = await self.redis.get(_owner_key(user_id))
        if not raw:
            return []
        try:
            entries = [InviteLink(**item) for item in json.loads(raw)]
        except (ValueError, TypeError):
            logger.warning(f"Dropping unreadable invite list of user '{user_id}'")
            await self.redis.delete(_owner_key(user_id))
            return []

        now = self.clock()
        alive = []
        for link in entries:
            # A spent link is gone from Redis even though it has not expired.
            if link.expires_at > now and await self.redis.get(_token_key(link.token)):
                alive.append(link)
        if len(alive) != len(entries):
            await self._store(user_id, alive)
        return alive

    async def current(self, user: UserDto) -> InviteLink:
        """The newest usable link, issuing one when none is left."""
        alive = await self.live(user.id)
        if alive:
            return alive[-1]
        link = await self.issue(user, force=True)
        assert link is not None  # force ignores the limit
        return link

    async def issue(self, user: UserDto, *, force: bool = False) -> Optional[InviteLink]:
        """A new link, or None when the user already has ``max_active`` live ones."""
        settings = await self.settings()
        alive = await self.live(user.id)
        if not force and len(alive) >= settings.max_active:
            return None

        ttl = settings.ttl_hours * 3600
        token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))
        link = InviteLink(token=token, expires_at=self.clock() + ttl)
        await self.redis.set(_token_key(token), str(user.id), ex=ttl)
        await self._store(user.id, [*alive, link])
        logger.info(f"{user.log} Issued one-time invite link for {settings.ttl_hours}h")
        return link

    async def owner(self, token: str) -> Optional[UserDto]:
        """Who issued a live link, without spending it."""
        raw = await self.redis.get(_token_key(token))
        return await self._user(raw)

    async def spend(self, token: str) -> Optional[UserDto]:
        """Spend a link; only one caller ever gets its owner back."""
        raw = await self.redis.getdel(_token_key(token))
        owner = await self._user(raw)
        if owner:
            logger.info(f"One-time invite link of '{owner.id}' used")
        return owner

    async def revoke_all(self, user_id: int) -> int:
        """Kill every live link of the user: a reset referral code means "the
        links I gave out must stop working", the one-time ones included."""
        links = await self.live(user_id)
        for link in links:
            await self.redis.delete(_token_key(link.token))
        await self._store(user_id, [])
        if links:
            logger.info(f"Revoked {len(links)} one-time invite link(s) of '{user_id}'")
        return len(links)

    async def url(self, link: InviteLink) -> str:
        return await self.bot_service.get_referral_url(link.token)

    async def referral_url(self, user: UserDto) -> str:
        """What every "invite" button shares: a one-time link while the feature
        is on, the permanent one otherwise."""
        if not (await self.settings()).enabled:
            return await self.bot_service.get_referral_url(user.referral_code)
        return await self.url(await self.current(user))

    async def _user(self, raw: Optional[str]) -> Optional[UserDto]:
        if not raw:
            return None
        try:
            return await self.user_dao.get_by_id(int(raw))
        except ValueError:
            return None

    async def _store(self, user_id: int, links: list[InviteLink]) -> None:
        if not links:
            await self.redis.delete(_owner_key(user_id))
            return
        ttl = max(1, int(max(link.expires_at for link in links) - self.clock()))
        payload = json.dumps([{"token": x.token, "expires_at": x.expires_at} for x in links])
        await self.redis.set(_owner_key(user_id), payload, ex=ttl)
