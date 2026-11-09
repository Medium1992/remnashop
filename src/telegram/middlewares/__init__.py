from aiogram import Router

from .access import AccessMiddleware
from .base import EventTypedMiddleware
from .channel import ChannelMiddleware
from .error import ErrorMiddleware
from .garbage import GarbageMiddleware
from .rules import RulesMiddleware
from .s_invites import SInviteMiddleware, Stage
from .throttling import ThrottlingMiddleware
from .user import UserMiddleware

__all__ = [
    "setup_middlewares",
]


def setup_middlewares(router: Router) -> None:
    outer_middlewares: list[EventTypedMiddleware] = [
        SInviteMiddleware(Stage.PEEK),
        AccessMiddleware(),
        ErrorMiddleware(),
        SInviteMiddleware(Stage.SPEND),
        UserMiddleware(),
        # Throttle before the heavier Rules/Channel checks (DB/Telegram API) so flood
        # is short-circuited early.
        ThrottlingMiddleware(),
        RulesMiddleware(),
        ChannelMiddleware(),
    ]

    inner_middlewares: list[EventTypedMiddleware] = [
        GarbageMiddleware(),
    ]

    for middleware in outer_middlewares:
        middleware.setup_outer(router=router)

    for middleware in inner_middlewares:
        middleware.setup_inner(router=router)
