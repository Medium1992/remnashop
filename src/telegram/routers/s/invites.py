"""The "s" additions to the user's invite screen: one-time links.

Hooked into the upstream screen at two points: its getter merges
``invite_extra`` into what it returns, and its window shows the note and the
"new link" button.
"""

from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common import Notifier, TranslatorRunner
from src.application.dto import MessagePayloadDto, TelegramUserDto, UserDto
from src.core.constants import USER_KEY
from src.infrastructure.services.s.invites import InviteLinks


async def invite_extra(
    invites: InviteLinks, user: UserDto, i18n: TranslatorRunner
) -> dict[str, Any]:
    settings = await invites.settings()
    if not settings.enabled:
        return {"s_invite": 0}

    await invites.current(user)  # issues one when none is left
    alive = await invites.live(user.id)
    now = invites.clock()
    # Newest first: the one the "copy" button gives.
    lines = [
        i18n.get(
            "msg-s-invite.link",
            n=number,
            url=await invites.url(link),
            # A string: a number would come out of Fluent with digit grouping.
            until=str(int(link.expires_at)),
            hours=link.hours_left(now),
        )
        for number, link in enumerate(reversed(alive), start=1)
    ]
    return {
        "s_invite": 1,
        "s_invite_links": "\n".join(lines),
        "s_invite_active": len(alive),
        "s_invite_max": settings.max_active,
        "s_invite_can_add": int(len(alive) < settings.max_active),
    }


@inject
async def on_new_invite(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    invites: FromDishka[InviteLinks],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    if await invites.issue(user) is None:
        settings = await invites.settings()
        await notifier.notify_user(
            user,
            payload=MessagePayloadDto(
                i18n_key="ntf-s-invites.limit",
                i18n_kwargs={"max_active": settings.max_active},
            ),
        )
