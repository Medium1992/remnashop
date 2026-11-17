"""The web cabinet's sign-in: only through its proxy, one-time links, sessions."""

import builtins
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional, Set
from urllib.parse import urlencode

import httpx
import pytest
from aiogram import Bot
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI

from src.application.common.dao import UserDao
from src.core.config import AppConfig
from src.core.utils import s_cabinet
from src.core.utils.s_cabinet import CabinetConfig
from src.infrastructure.services.s.cabinet_sessions import CabinetSessions, Grant
from src.web.s_cabinet import mount_cabinet
from src.web.s_cabinet.auth import verify_init_data

SECRET = "s" * 40
BOT_TOKEN = "123456:TEST-token"


class FakeRedis:
    """The few commands the sessions use, with expiry on a clock of our own."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.expires: dict[str, float] = {}
        self.now = 1000.0

    def _alive(self, key: str) -> bool:
        if key in self.expires and self.expires[key] <= self.now:
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> None:
        self.data[key] = value.encode() if isinstance(value, str) else value
        self.expires.pop(key, None)
        if ex:
            self.expires[key] = self.now + ex

    async def get(self, key: str) -> Any:
        return self.data[key] if self._alive(key) else None

    async def getdel(self, key: str) -> Any:
        value = await self.get(key)
        self.data.pop(key, None)
        return value

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)

    async def exists(self, key: str) -> int:
        return int(self._alive(key))

    async def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        if self._alive(key) and not (nx and key in self.expires):
            self.expires[key] = self.now + seconds

    async def ttl(self, key: str) -> int:
        if not self._alive(key):
            return -2
        return int(self.expires[key] - self.now) if key in self.expires else -1

    async def sadd(self, key: str, *values: str) -> None:
        self.data.setdefault(key, builtins.set()).update(v.encode() for v in values)

    async def srem(self, key: str, *values: str) -> None:
        self.data.get(key, builtins.set()).difference_update(v.encode() for v in values)

    async def smembers(self, key: str) -> Set[bytes]:
        return builtins.set(self.data.get(key, ())) if self._alive(key) else builtins.set()

    async def incr(self, key: str) -> int:
        value = int(self.data.get(key, 0)) + 1 if self._alive(key) else 1
        self.data[key] = value
        return value

    def pipeline(self, transaction: bool = True) -> "Pipe":
        return Pipe(self)


class Pipe:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.calls: list[Any] = []

    async def __aenter__(self) -> "Pipe":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def queue(*args: Any, **kwargs: Any) -> None:
            self.calls.append(getattr(self.redis, name)(*args, **kwargs))

        return queue

    async def execute(self) -> list[Any]:
        return [await call for call in self.calls]


def user(user_id: int = 7, telegram_id: int = 700, blocked: bool = False) -> Any:
    return SimpleNamespace(
        id=user_id, telegram_id=telegram_id, is_blocked=blocked, log=f"[{user_id}]"
    )


class Users:
    def __init__(self, *users: Any) -> None:
        self.users = list(users)

    async def get_by_id(self, user_id: int) -> Any:
        return next((u for u in self.users if u.id == user_id), None)

    async def get_by_telegram_id(self, telegram_id: int) -> Any:
        return next((u for u in self.users if u.telegram_id == telegram_id), None)


class FakeBot:
    async def me(self) -> Any:
        return SimpleNamespace(username="shop_bot")


def init_data(telegram_id: int, auth_date: Optional[int] = None, token: str = BOT_TOKEN) -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "user": json.dumps({"id": telegram_id, "first_name": "A"}),
        "query_id": "q",
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def sessions(redis: FakeRedis) -> CabinetSessions:
    return CabinetSessions(redis, b"k" * 32)  # type: ignore[arg-type]


@pytest.fixture
def users() -> Users:
    return Users(user(), user(8, 800, blocked=True))


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, sessions: CabinetSessions, users: Users
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("CABINET_DOMAIN", "cabinet.example.com")
    monkeypatch.setenv("CABINET_PROXY_SECRET", SECRET)
    s_cabinet.cabinet_config.cache_clear()

    class Fakes(Provider):
        @provide(scope=Scope.APP)
        def config(self) -> CabinetConfig:
            return CabinetConfig(domain="cabinet.example.com", proxy_secret=SECRET)

        @provide(scope=Scope.APP)
        def store(self) -> CabinetSessions:
            return sessions

        @provide(scope=Scope.APP)
        def app_config(self) -> AppConfig:
            token = SimpleNamespace(get_secret_value=lambda: BOT_TOKEN)
            return SimpleNamespace(bot=SimpleNamespace(token=token))  # type: ignore[return-value]

        @provide(scope=Scope.APP)
        def bot(self) -> Bot:
            return FakeBot()  # type: ignore[return-value]

        @provide(scope=Scope.REQUEST)
        def user_dao(self) -> UserDao:
            return users  # type: ignore[return-value]

    app = FastAPI()
    mount_cabinet(app)
    container = make_async_container(Fakes())
    setup_dishka(container, app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        headers={"X-Cabinet-Proxy-Secret": SECRET, "X-Cabinet-Client-IP": "203.0.113.5"},
    ) as http:
        yield http
    await container.close()
    s_cabinet.cabinet_config.cache_clear()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- the proxy -----------------------------------------------------------------


async def test_without_the_proxy_secret_nothing_is_there(client: httpx.AsyncClient) -> None:
    for headers in ({"X-Cabinet-Proxy-Secret": ""}, {"X-Cabinet-Proxy-Secret": "x" * 40}):
        response = await client.get("/api/v1/auth/methods", headers=headers)
        assert response.status_code == 404


async def test_the_methods_name_the_bot(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/auth/methods")).json()
    assert body["telegram_webapp"] and body["telegram_deeplink"]
    assert not body["email"] and body["oauth"] == []
    assert body["bot_username"] == "shop_bot"


def test_nothing_is_mounted_without_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CABINET_DOMAIN", raising=False)
    monkeypatch.delenv("CABINET_PROXY_SECRET", raising=False)
    s_cabinet.cabinet_config.cache_clear()
    app = FastAPI()
    mount_cabinet(app)
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/auth")]
    monkeypatch.setenv("CABINET_DOMAIN", "https://cabinet.example.com")
    monkeypatch.setenv("CABINET_PROXY_SECRET", "short")
    s_cabinet.cabinet_config.cache_clear()
    assert s_cabinet.cabinet_config() is None
    s_cabinet.cabinet_config.cache_clear()


# -- the Mini App ----------------------------------------------------------------


def test_init_data_is_checked() -> None:
    now = int(time.time())
    assert verify_init_data(init_data(700), BOT_TOKEN, now) == 700
    assert verify_init_data(init_data(700, token="1:other"), BOT_TOKEN, now) is None
    assert verify_init_data(init_data(700, auth_date=now - 2 * 86400), BOT_TOKEN, now) is None
    tampered = init_data(700).replace("700", "701")
    assert verify_init_data(tampered, BOT_TOKEN, now) is None
    assert verify_init_data("", BOT_TOKEN, now) is None


async def test_the_mini_app_signs_in_a_bot_user(
    client: httpx.AsyncClient, sessions: CabinetSessions
) -> None:
    response = await client.post("/api/v1/auth/telegram", json={"init_data": init_data(700)})
    assert response.status_code == 200
    session = await sessions.verify(response.json()["access_token"])
    assert session is not None and session.user_id == 7 and session.mini_app
    assert "cabinet_refresh_token" in response.cookies


async def test_the_mini_app_refuses(client: httpx.AsyncClient) -> None:
    bad = await client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(700, token="1:x")}
    )
    assert bad.status_code == 401 and bad.json()["code"] == "INVALID_INIT_DATA"
    stranger = await client.post("/api/v1/auth/telegram", json={"init_data": init_data(999)})
    assert stranger.status_code == 403 and stranger.json()["code"] == "USER_NOT_FOUND"
    blocked = await client.post("/api/v1/auth/telegram", json={"init_data": init_data(800)})
    assert blocked.status_code == 403 and blocked.json()["code"] == "USER_BLOCKED"


# -- links from the bot -------------------------------------------------------------


async def test_a_link_from_the_menu_works_once(
    client: httpx.AsyncClient, sessions: CabinetSessions
) -> None:
    token = await sessions.issue_grant(Grant(user_id=7))
    first = await client.post("/api/v1/auth/telegram/signin", json={"token": token})
    assert first.status_code == 200
    session = await sessions.verify(first.json()["access_token"])
    assert session is not None and session.user_id == 7 and not session.mini_app
    again = await client.post("/api/v1/auth/telegram/signin", json={"token": token})
    assert again.status_code == 401 and again.json()["code"] == "DEEPLINK_EXPIRED"


async def test_a_link_runs_out(
    client: httpx.AsyncClient, sessions: CabinetSessions, redis: FakeRedis
) -> None:
    token = await sessions.issue_grant(Grant(user_id=7))
    redis.now += 121
    response = await client.post("/api/v1/auth/telegram/signin", json={"token": token})
    assert response.json()["code"] == "DEEPLINK_EXPIRED"


async def test_a_sign_in_started_on_the_site_works_only_in_that_browser(
    client: httpx.AsyncClient, sessions: CabinetSessions
) -> None:
    started = await client.post("/api/v1/auth/telegram/deeplink", json={})
    bot_url = started.json()["bot_url"]
    assert bot_url.startswith("https://t.me/shop_bot?start=webauth_")
    ticket = bot_url.split("webauth_", 1)[1]
    # the bot's side
    binding_hash = await sessions.take_start(ticket)
    assert binding_hash is not None and await sessions.take_start(ticket) is None
    token = await sessions.issue_grant(Grant(user_id=7, binding_hash=binding_hash))

    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="https://testserver",
        headers=dict(client.headers),
    ) as other_browser:
        refused = await other_browser.post("/api/v1/auth/telegram/signin", json={"token": token})
        assert refused.json()["code"] == "DEEPLINK_OTHER_BROWSER"

    # left unspent for the right browser
    signed = await client.post("/api/v1/auth/telegram/signin", json={"token": token})
    assert signed.status_code == 200


async def test_a_blocked_user_is_not_signed_in_by_a_link(
    client: httpx.AsyncClient, sessions: CabinetSessions
) -> None:
    token = await sessions.issue_grant(Grant(user_id=8))
    response = await client.post("/api/v1/auth/telegram/signin", json={"token": token})
    assert response.status_code == 403 and response.json()["code"] == "USER_BLOCKED"


# -- the session -------------------------------------------------------------------


async def _signed_in(client: httpx.AsyncClient, sessions: CabinetSessions) -> str:
    token = await sessions.issue_grant(Grant(user_id=7))
    response = await client.post("/api/v1/auth/telegram/signin", json={"token": token})
    return str(response.json()["access_token"])


async def test_refresh_needs_the_csrf_header_and_rotates(
    client: httpx.AsyncClient, sessions: CabinetSessions, redis: FakeRedis
) -> None:
    await _signed_in(client, sessions)
    old = client.cookies.get("cabinet_refresh_token")
    without = await client.post("/api/v1/auth/refresh")
    assert without.status_code == 401
    csrf = {"X-Csrf-Token": client.cookies.get("cabinet_csrf_token") or ""}
    rotated = await client.post("/api/v1/auth/refresh", headers=csrf)
    assert rotated.status_code == 200 and rotated.json()["access_token"]
    assert client.cookies.get("cabinet_refresh_token") != old
    # the old one lasts only the grace
    redis.now += 31
    replay = await sessions.refresh(old or "")
    assert replay is None


async def test_sign_out_ends_the_tokens(
    client: httpx.AsyncClient, sessions: CabinetSessions
) -> None:
    access = await _signed_in(client, sessions)
    assert (await client.post("/api/v1/auth/logout", headers=bearer(access))).status_code == 200
    assert await sessions.verify(access) is None
    again = await client.post("/api/v1/auth/logout", headers=bearer(access))
    assert again.status_code == 401


async def test_sign_out_elsewhere_keeps_this_session(
    client: httpx.AsyncClient, sessions: CabinetSessions
) -> None:
    other = await _signed_in(client, sessions)
    this = await _signed_in(client, sessions)
    response = await client.post(
        "/api/v1/auth/sessions/actions/revoke-others", headers=bearer(this)
    )
    assert response.status_code == 204
    assert await sessions.verify(this) is not None
    assert await sessions.verify(other) is None


async def test_a_token_of_another_key_is_refused(
    client: httpx.AsyncClient, sessions: CabinetSessions, redis: FakeRedis
) -> None:
    await _signed_in(client, sessions)
    forged = CabinetSessions(redis, b"x" * 32)  # type: ignore[arg-type]
    _, issued = await forged.open(7, mini_app=False)
    response = await client.post("/api/v1/auth/logout", headers=bearer(issued.access_token))
    assert response.status_code == 401


async def test_sign_in_is_rate_limited(client: httpx.AsyncClient) -> None:
    codes = [
        (await client.post("/api/v1/auth/telegram/signin", json={"token": "x"})).status_code
        for _ in range(21)
    ]
    assert codes[-1] == 429 and set(codes[:20]) == {401}


async def test_email_and_oauth_are_off(client: httpx.AsyncClient) -> None:
    email = await client.post("/api/v1/auth/email/code", json={})
    assert email.json()["code"] == "CABINET_EMAIL_DISABLED"
    oauth = await client.post("/api/v1/auth/oauth/google/authorize", json={})
    assert oauth.json()["code"] == "OAUTH_PROVIDER_DISABLED"
