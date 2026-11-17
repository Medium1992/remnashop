"""Signing in to the cabinet: the Mini App, a link from the bot, sign-out.

Only people who already use the bot get in: the cabinet adds no sign-up, so
the bot's own registration rules (access mode, referrals, blacklist) stay the
only way in. Email and OAuth sign-in are not offered.
"""

import hashlib
import hmac
import json
import time
from typing import Any, Final, Optional
from urllib.parse import parse_qsl

from aiogram import Bot
from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from src.application.common.dao import UserDao
from src.core.config import AppConfig
from src.infrastructure.services.s.cabinet_sessions import (
    START_TTL,
    CabinetSessions,
    binding_matches,
)

from .deps import (
    BINDING_COOKIE,
    REFRESH_COOKIE,
    CurrentActor,
    check_csrf,
    clear_binding_cookie,
    clear_session_cookies,
    limit,
    set_binding_cookie,
    set_session_cookies,
    token_body,
)
from .errors import CabinetError, unauthorized

router = APIRouter(prefix="/auth")

START_PREFIX: Final[str] = "webauth_"
# initData of a Mini App launch: the app signs in with it again whenever its
# access token runs out, so it is good for as long as the app stays open
INIT_DATA_MAX_AGE: Final[int] = 24 * 3600
CLOCK_SKEW: Final[int] = 60


def verify_init_data(init_data: str, bot_token: str, now: int) -> Optional[int]:
    """The Telegram id inside initData signed by the bot's token, or None."""
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    presented = fields.pop("hash", "")
    if not presented:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented, expected):
        return None
    try:
        auth_date = int(fields["auth_date"])
        telegram_id = int(json.loads(fields["user"])["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if not -CLOCK_SKEW <= now - auth_date <= INIT_DATA_MAX_AGE:
        return None
    return telegram_id


async def bot_username(bot: Bot) -> Optional[str]:
    try:
        return (await bot.me()).username
    except Exception:
        return None


class InitDataBody(BaseModel):
    init_data: str


class TokenBody(BaseModel):
    token: str


@router.get("/methods")
@inject
async def methods(bot: FromDishka[Bot]) -> dict[str, Any]:
    return {
        "telegram_webapp": True,
        "telegram_deeplink": True,
        "email": False,
        "oauth": [],
        "allowed_email_domains": None,
        "captcha": None,
        "bot_username": await bot_username(bot),
    }


@router.post("/telegram")
@inject
async def mini_app_sign_in(
    body: InitDataBody,
    request: Request,
    response: Response,
    config: FromDishka[AppConfig],
    sessions: FromDishka[CabinetSessions],
    user_dao: FromDishka[UserDao],
) -> dict[str, object]:
    await limit(sessions, request, "telegram", 30)
    telegram_id = verify_init_data(
        body.init_data, config.bot.token.get_secret_value(), int(time.time())
    )
    if telegram_id is None:
        raise CabinetError(401, "INVALID_INIT_DATA", "Invalid Telegram init data")
    user = await user_dao.get_by_telegram_id(telegram_id)
    if user is None:
        raise CabinetError(403, "USER_NOT_FOUND", "Open the bot first")
    if user.is_blocked:
        raise CabinetError(403, "USER_BLOCKED", "User is blocked")
    _, issued = await sessions.open(user.id, mini_app=True)
    set_session_cookies(response, issued)
    return token_body(issued)


@router.post("/telegram/deeplink")
@inject
async def deeplink(
    request: Request,
    response: Response,
    bot: FromDishka[Bot],
    sessions: FromDishka[CabinetSessions],
) -> dict[str, object]:
    await limit(sessions, request, "deeplink", 20)
    username = await bot_username(bot)
    if not username:
        raise CabinetError(503, "OAUTH_PROVIDER_UNREACHABLE", "The bot is unreachable")
    token, binding = await sessions.issue_start()
    set_binding_cookie(response, binding, START_TTL)
    return {
        "bot_url": f"https://t.me/{username}?start={START_PREFIX}{token}",
        "expires_in": START_TTL,
    }


@router.post("/telegram/signin")
@inject
async def bot_sign_in(
    body: TokenBody,
    request: Request,
    response: Response,
    sessions: FromDishka[CabinetSessions],
    user_dao: FromDishka[UserDao],
) -> dict[str, object]:
    await limit(sessions, request, "signin", 20)
    grant = await sessions.peek_grant(body.token)
    if grant is None:
        raise CabinetError(401, "DEEPLINK_EXPIRED", "The sign-in link has expired")
    # left unspent: the right browser can still use it
    if not binding_matches(request.cookies.get(BINDING_COOKIE), grant.binding_hash):
        raise CabinetError(401, "DEEPLINK_OTHER_BROWSER", "Opened in another browser")
    if await sessions.take_grant(body.token) is None:
        raise CabinetError(401, "DEEPLINK_EXPIRED", "The sign-in link has expired")
    user = await user_dao.get_by_id(grant.user_id)
    if user is None:
        raise CabinetError(401, "DEEPLINK_EXPIRED", "The sign-in link has expired")
    if user.is_blocked:
        raise CabinetError(403, "USER_BLOCKED", "User is blocked")
    _, issued = await sessions.open(user.id, mini_app=False)
    clear_binding_cookie(response)
    set_session_cookies(response, issued)
    return token_body(issued)


@router.post("/refresh")
@inject
async def refresh(
    request: Request,
    response: Response,
    sessions: FromDishka[CabinetSessions],
) -> dict[str, object]:
    await limit(sessions, request, "refresh", 60)
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise unauthorized("No refresh token")
    check_csrf(request)
    result = await sessions.refresh(token)
    if result is None:
        raise unauthorized("Session expired")
    _, issued = result
    set_session_cookies(response, issued)
    return token_body(issued)


@router.post("/logout")
@inject
async def logout(
    actor: CurrentActor,
    response: Response,
    sessions: FromDishka[CabinetSessions],
) -> None:
    await sessions.close(actor.session)
    clear_session_cookies(response)


@router.post("/sessions/actions/revoke-others", status_code=204)
@inject
async def revoke_others(
    actor: CurrentActor,
    request: Request,
    sessions: FromDishka[CabinetSessions],
) -> None:
    await limit(sessions, request, "revoke", 5)
    await sessions.close_others(actor.session)


# Not offered: answered as switched off, the way the cabinet expects.
@router.post("/email/code")
@router.post("/email/verify")
async def email_off() -> None:
    raise CabinetError(403, "CABINET_EMAIL_DISABLED", "Email sign-in is off")


@router.post("/oauth/{provider}/authorize")
@router.post("/oauth/{provider}/callback")
async def oauth_off(provider: str) -> None:
    raise CabinetError(403, "OAUTH_PROVIDER_DISABLED", "Provider is off")
