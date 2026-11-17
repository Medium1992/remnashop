"""What every cabinet route checks: the cabinet's proxy, the session, limits."""

import hmac
import ipaddress
from dataclasses import dataclass
from typing import Annotated, Final

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import Depends, Request, Response

from src.application.common.dao import UserDao
from src.application.dto import UserDto
from src.core.utils.s_cabinet import CabinetConfig
from src.infrastructure.services.s.cabinet_sessions import (
    ACCESS_TTL,
    CabinetSessions,
    Issued,
    Session,
)

from .errors import CabinetError, unauthorized

PROXY_SECRET_HEADER: Final[str] = "x-cabinet-proxy-secret"
CLIENT_IP_HEADER: Final[str] = "x-cabinet-client-ip"
REFRESH_COOKIE: Final[str] = "cabinet_refresh_token"
CSRF_COOKIE: Final[str] = "cabinet_csrf_token"
BINDING_COOKIE: Final[str] = "deeplink_binding"
# the cabinet's nginx maps /api/v1/ to /api/ in cookie paths
AUTH_PATH: Final[str] = "/api/v1/auth"


@inject
async def require_cabinet_proxy(
    request: Request,
    config: FromDishka[CabinetConfig],
) -> None:
    """Only the cabinet's own nginx, which adds the secret, reaches these routes;
    anyone else gets the 404 of a route that isn't there."""
    presented = request.headers.get(PROXY_SECRET_HEADER, "")
    if not hmac.compare_digest(presented.encode(), config.proxy_secret.encode()):
        raise CabinetError(404, "NOT_FOUND", "Not Found")


def client_ip(request: Request) -> str:
    # set by the cabinet's nginx, trusted because the secret came with it
    raw = request.headers.get(CLIENT_IP_HEADER, "")
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return "unknown"


async def limit(sessions: CabinetSessions, request: Request, name: str, per_minute: int) -> None:
    if not await sessions.hit(name, client_ip(request), per_minute):
        raise CabinetError(429, "RATE_LIMITED", "Too many requests")


@dataclass(frozen=True)
class Actor:
    user: UserDto
    session: Session


@inject
async def current_actor(
    request: Request,
    sessions: FromDishka[CabinetSessions],
    user_dao: FromDishka[UserDao],
) -> Actor:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()
    session = await sessions.verify(token.strip())
    if session is None:
        raise unauthorized("Invalid or expired token")
    user = await user_dao.get_by_id(session.user_id)
    if user is None:
        raise unauthorized("User not found")
    if user.is_blocked:
        raise CabinetError(403, "USER_BLOCKED", "User is blocked")
    return Actor(user=user, session=session)


CurrentActor = Annotated[Actor, Depends(current_actor)]


def check_csrf(request: Request) -> None:
    """Cookie-authenticated calls carry the CSRF cookie's value in a header."""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise unauthorized("CSRF check failed")


def set_session_cookies(response: Response, issued: Issued) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=issued.refresh_ttl,
        path=AUTH_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf_token,
        max_age=issued.refresh_ttl,
        path="/",
        httponly=False,
        secure=True,
        samesite="strict",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=AUTH_PATH, secure=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, samesite="strict")


def set_binding_cookie(response: Response, binding: str, max_age: int) -> None:
    response.set_cookie(
        BINDING_COOKIE,
        binding,
        max_age=max_age,
        path=AUTH_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def clear_binding_cookie(response: Response) -> None:
    response.delete_cookie(BINDING_COOKIE, path=AUTH_PATH, secure=True, samesite="lax")


def token_body(issued: Issued) -> dict[str, object]:
    return {"access_token": issued.access_token, "expires_in": ACCESS_TTL}
