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

from src.application.common import Notifier
from src.application.dto import MessagePayloadDto, TelegramUserDto, UserDto
from src.core.constants import USER_KEY
from src.infrastructure.services.s.invites import InviteLinks


async def invite_extra(invites: InviteLinks, user: UserDto) -> dict[str, Any]:
    settings = await invites.settings()
    if not settings.enabled:
        return {"s_invite": 0}

    link = await invites.current(user)
    active = len(await invites.live(user.id))
    return {
        "s_invite": 1,
        "s_invite_hours": link.hours_left(invites.clock()),
        # A string: a number would come out of Fluent with digit grouping.
        "s_invite_until": str(int(link.expires_at)),
        "s_invite_active": active,
        "s_invite_max": settings.max_active,
        "s_invite_can_add": int(active < settings.max_active),
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
