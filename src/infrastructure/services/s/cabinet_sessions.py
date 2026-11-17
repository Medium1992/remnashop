"""Cabinet sessions and one-time sign-in tokens, kept in Redis.

A session is a short access token (a signed JWT, in the page's memory) and a
refresh token (an httponly cookie). Redis keeps only hashes of the tokens: a
copy of Redis signs nobody in.

Sign-in tokens are spent once (GETDEL) and live for minutes:

- a grant: the bot made it for a user, the cabinet trades it for a session;
- a start ticket: the cabinet made it for the "sign in with the bot" button,
  the bot trades it for a grant bound to the browser that asked.
"""

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Final, Optional, cast

import jwt
from redis.asyncio import Redis

PREFIX: Final[str] = "s:cabinet:"
ACCESS_TTL: Final[int] = 15 * 60
REFRESH_TTL: Final[int] = 30 * 24 * 3600
# The Mini App signs in again with initData whenever it needs to.
MINI_APP_TTL: Final[int] = 7 * 24 * 3600
# A rotated refresh token still works this long: two tabs refreshing at once.
ROTATION_GRACE: Final[int] = 30
GRANT_TTL: Final[int] = 120
START_TTL: Final[int] = 300
AUDIENCE: Final[str] = "s-cabinet"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def binding_matches(binding: Optional[str], binding_hash: Optional[str]) -> bool:
    if binding_hash is None:
        return True
    return binding is not None and hmac.compare_digest(digest(binding), binding_hash)


@dataclass(frozen=True)
class Session:
    sid: str
    user_id: int
    mini_app: bool


@dataclass(frozen=True)
class Issued:
    access_token: str
    refresh_token: str
    csrf_token: str
    refresh_ttl: int


@dataclass(frozen=True)
class Grant:
    user_id: int
    # set when the sign-in started in a browser: only that browser may use it
    binding_hash: Optional[str] = None


class CabinetSessions:
    def __init__(self, redis: Redis, key: bytes) -> None:
        self.redis = redis
        self.key = key

    # -- access tokens -----------------------------------------------------

    def access_token(self, session: Session) -> str:
        now = int(time.time())
        payload = {
            "sub": str(session.user_id),
            "sid": session.sid,
            "mini": session.mini_app,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + ACCESS_TTL,
        }
        return jwt.encode(payload, self.key, algorithm="HS256")

    async def verify(self, token: str) -> Optional[Session]:
        try:
            payload = jwt.decode(token, self.key, algorithms=["HS256"], audience=AUDIENCE)
            user_id, sid = int(payload["sub"]), str(payload["sid"])
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
            return None
        # a session ended (sign-out, "sign out elsewhere") ends its tokens at once
        if not await self.redis.exists(self._session_key(sid)):
            return None
        return Session(sid=sid, user_id=user_id, mini_app=bool(payload.get("mini")))

    # -- sessions ----------------------------------------------------------

    def _session_key(self, sid: str) -> str:
        return f"{PREFIX}session:{sid}"

    def _refresh_key(self, refresh_hash: str) -> str:
        return f"{PREFIX}refresh:{refresh_hash}"

    def _user_key(self, user_id: int) -> str:
        return f"{PREFIX}user:{user_id}"

    async def open(self, user_id: int, mini_app: bool) -> tuple[Session, Issued]:
        session = Session(sid=secrets.token_urlsafe(16), user_id=user_id, mini_app=mini_app)
        ttl = MINI_APP_TTL if mini_app else REFRESH_TTL
        refresh = secrets.token_urlsafe(32)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(self._session_key(session.sid), json.dumps(asdict(session)), ex=ttl)
            pipe.set(self._refresh_key(digest(refresh)), session.sid, ex=ttl)
            pipe.sadd(self._user_key(user_id), session.sid)
            pipe.expire(self._user_key(user_id), REFRESH_TTL)
            await pipe.execute()
        return session, self._issued(session, refresh, ttl)

    def _issued(self, session: Session, refresh: str, ttl: int) -> Issued:
        return Issued(
            access_token=self.access_token(session),
            refresh_token=refresh,
            csrf_token=secrets.token_urlsafe(24),
            refresh_ttl=ttl,
        )

    async def refresh(self, refresh: str) -> Optional[tuple[Session, Issued]]:
        """A new access token and a new refresh token for a valid refresh token."""
        old_key = self._refresh_key(digest(refresh))
        sid = await self.redis.get(old_key)
        if sid is None:
            return None
        sid = sid.decode() if isinstance(sid, bytes) else str(sid)
        raw = await self.redis.get(self._session_key(sid))
        if raw is None:
            await self.redis.delete(old_key)
            return None
        session = Session(**json.loads(raw))
        ttl = int(await self.redis.ttl(self._session_key(sid)))
        if ttl <= 0:
            return None
        new = secrets.token_urlsafe(32)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(self._refresh_key(digest(new)), sid, ex=ttl)
            # the old one stays a moment for a second tab refreshing at once
            pipe.expire(old_key, min(ROTATION_GRACE, ttl))
            await pipe.execute()
        return session, self._issued(session, new, ttl)

    async def close(self, session: Session) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._session_key(session.sid))
            pipe.srem(self._user_key(session.user_id), session.sid)
            await pipe.execute()

    async def close_others(self, session: Session) -> int:
        members = self.redis.smembers(self._user_key(session.user_id))
        sids = [
            s.decode() if isinstance(s, bytes) else str(s)
            for s in await cast(Awaitable[set[Any]], members)
        ]
        others = [sid for sid in sids if sid != session.sid]
        if others:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.delete(*[self._session_key(sid) for sid in others])
                pipe.srem(self._user_key(session.user_id), *others)
                await pipe.execute()
        return len(others)

    # -- one-time sign-in tokens ---------------------------------------------

    async def issue_grant(self, grant: Grant) -> str:
        token = secrets.token_urlsafe(32)
        await self.redis.set(
            f"{PREFIX}grant:{digest(token)}", json.dumps(asdict(grant)), ex=GRANT_TTL
        )
        return token

    async def peek_grant(self, token: str) -> Optional[Grant]:
        raw = await self.redis.get(f"{PREFIX}grant:{digest(token)}")
        return None if raw is None else Grant(**json.loads(raw))

    async def take_grant(self, token: str) -> Optional[Grant]:
        raw = await self.redis.getdel(f"{PREFIX}grant:{digest(token)}")
        return None if raw is None else Grant(**json.loads(raw))

    async def issue_start(self) -> tuple[str, str]:
        """A start ticket for the bot and the binding for this browser's cookie."""
        token, binding = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        await self.redis.set(f"{PREFIX}start:{digest(token)}", digest(binding), ex=START_TTL)
        return token, binding

    async def take_start(self, token: str) -> Optional[str]:
        """The binding hash of a start ticket, spent."""
        raw = await self.redis.getdel(f"{PREFIX}start:{digest(token)}")
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- rate limits ---------------------------------------------------------

    async def hit(self, name: str, who: str, limit: int, window: int = 60) -> bool:
        """Counts a call; False once ``limit`` calls were made in the window."""
        key = f"{PREFIX}rate:{name}:{who}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= limit
