"""Rules as text in Telegram, not only a link (docs/s/README.md).

The admin sends the rules in the same window where upstream asks for the
link: a link keeps upstream's behaviour, any other text becomes the rules,
formatted as it was typed. Then the acceptance notice, ``/rules`` and the
"📜 Правила" menu button show the text itself.
"""

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.widgets.kbd import Button
from dishka import AsyncContainer, FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common import Notifier
from src.application.common.dao import SettingsDao
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.application.use_cases.s.settings import SetRulesText
from src.core.constants import CONTAINER_KEY, USER_KEY
from src.core.exceptions import PermissionDeniedError
from src.core.utils.validators import is_valid_url
from src.telegram.s_rules import rules_payload, rules_text


def is_rules_text(message: Message) -> bool:
    """What the admin sent is the rules, not upstream's link."""
    raw = (message.text or "").strip()
    return bool(raw) and not is_valid_url(raw)


async def on_rules_text_input(message: Message, dialog_manager: DialogManager) -> None:
    """Called from upstream's link input when the message is not a link."""
    container: AsyncContainer = dialog_manager.middleware_data[CONTAINER_KEY]
    notifier = await container.get(Notifier)
    set_rules_text = await container.get(SetRulesText)
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    html = (message.text or "").strip()
    if html != "-":
        html = message.html_text.strip()
        # Shown back first: what Telegram refuses (too long) never gets saved.
        try:
            await message.answer(html)
        except TelegramBadRequest as error:
            await notifier.notify_user(
                user,
                payload=MessagePayloadDto(
                    i18n_key="ntf-s-rules.refused", i18n_kwargs={"detail": error.message}
                ),
            )
            return
    try:
        text = await set_rules_text(user, html)
    except PermissionDeniedError:
        await notifier.notify_user(user, i18n_key="ntf-s-texts.denied")
        return
    await notifier.notify_user(
        user, i18n_key="ntf-s-rules.saved" if text else "ntf-s-rules.cleared"
    )


@inject
async def on_show_rules(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    settings_dao: FromDishka[SettingsDao],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    text = rules_text(await settings_dao.get())
    if text:
        await notifier.notify_user(user, payload=rules_payload(text, accept=False))
