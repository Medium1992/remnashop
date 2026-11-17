"""The web cabinet from the bot (docs/s/README.md, src/core/utils/s_cabinet.py).

- "🌐 Личный кабинет" in the main menu: a one-time link that signs this person
  in, good for two minutes; the message goes away when it runs out.
- ``/start webauth_<ticket>``: the cabinet's "sign in with Telegram" button
  sent the person here; the link that comes back works only in the browser
  that asked.
"""

from typing import Final, Optional

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier, TranslatorRunner
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.core.constants import USER_KEY
from src.core.utils.s_cabinet import CabinetConfig, cabinet_config
from src.infrastructure.services.s.cabinet_sessions import GRANT_TTL, CabinetSessions, Grant

START_PREFIX: Final[str] = "webauth_"

router = Router(name=__name__)


def sign_in_url(config: CabinetConfig, token: str) -> str:
    # a fragment, not a query: it never reaches a server log or a Referer
    return f"{config.url}/login/bot#token={token}"


def _markup(
    i18n: TranslatorRunner, config: CabinetConfig, token: str, mini_app: bool
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=i18n.get("btn-s-cabinet.open"), url=sign_in_url(config, token)
            )
        ]
    ]
    if mini_app:
        rows.append(
            [
                InlineKeyboardButton(
                    text=i18n.get("btn-s-cabinet.open-in-telegram"),
                    web_app=WebAppInfo(url=config.url),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_link(
    user: TelegramUserDto,
    sessions: CabinetSessions,
    notifier: Notifier,
    i18n: TranslatorRunner,
    binding_hash: Optional[str],
) -> None:
    config = cabinet_config()
    if config is None:
        return
    token = await sessions.issue_grant(Grant(user_id=user.id, binding_hash=binding_hash))
    bound = binding_hash is not None
    await notifier.notify_user(
        user,
        payload=MessagePayloadDto(
            i18n_key="ntf-s-cabinet.link-browser" if bound else "ntf-s-cabinet.link",
            i18n_kwargs={"minutes": GRANT_TTL // 60},
            reply_markup=_markup(i18n, config, token, mini_app=not bound),
            delete_after=GRANT_TTL,
        ),
    )
    logger.info(f"{user.log} Cabinet sign-in link sent ({'browser' if bound else 'menu'})")


@inject
async def on_cabinet(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    sessions: FromDishka[CabinetSessions],
    notifier: FromDishka[Notifier],
    i18n: FromDishka[TranslatorRunner],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    if not await sessions.hit("menu-link", str(user.telegram_id), 5):
        await callback.answer(i18n.get("ntf-s-cabinet.too-often"), show_alert=True)
        return
    await send_link(user, sessions, notifier, i18n, binding_hash=None)
    await callback.answer()


# FromDishka here comes from auto_inject (src/__main__.py)
@router.message(CommandStart(deep_link=True, ignore_case=True), F.text.contains(START_PREFIX))
async def on_web_sign_in(
    message: Message,
    command: CommandObject,
    user: TelegramUserDto,
    sessions: FromDishka[CabinetSessions],
    notifier: FromDishka[Notifier],
    i18n: FromDishka[TranslatorRunner],
) -> None:
    ticket = (command.args or "").removeprefix(START_PREFIX)
    binding_hash = await sessions.take_start(ticket) if ticket else None
    if binding_hash is None or cabinet_config() is None:
        await notifier.notify_user(user, i18n_key="ntf-s-cabinet.expired")
        return
    await send_link(user, sessions, notifier, i18n, binding_hash=binding_hash)
