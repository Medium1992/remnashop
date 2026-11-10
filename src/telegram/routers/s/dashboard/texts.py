"""Editing the bot's texts: find a text, send a new one, see it, save it.

The new text is sent back as a real message before it can be saved: what
Telegram refuses to show (broken tags) never reaches the users. Saving and
resetting take a second press, as deleting a plan does.
"""

from html import escape
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common import Notifier
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.core.constants import USER_KEY
from src.core.exceptions import PermissionDeniedError
from src.infrastructure.services.s.texts import TextError, TextOverrides
from src.telegram.s_states import SDashboard
from src.telegram.utils import is_double_click

_KEYS = "s_text_keys"
_MODE = "s_text_mode"
_QUERY = "s_text_query"
_KEY = "s_text_key"
_DRAFT = "s_text_draft"


def _user(dialog_manager: DialogManager) -> TelegramUserDto:
    return dialog_manager.middleware_data[USER_KEY]  # type: ignore[no-any-return]


async def _say(notifier: Notifier, user: TelegramUserDto, key: str, **kwargs: Any) -> None:
    await notifier.notify_user(user, payload=MessagePayloadDto(i18n_key=key, i18n_kwargs=kwargs))


async def _refuse(notifier: Notifier, user: TelegramUserDto, error: TextError) -> None:
    await _say(notifier, user, f"ntf-s-texts.{error.reason}", detail=escape(error.detail) or "—")


# ── getters ──────────────────────────────────────────────────────────────────


@inject
async def texts_getter(
    dialog_manager: DialogManager, texts: FromDishka[TextOverrides], **kwargs: Any
) -> dict[str, Any]:
    return {"texts_changed": len(texts.changed())}


@inject
async def text_list_getter(
    dialog_manager: DialogManager, texts: FromDishka[TextOverrides], **kwargs: Any
) -> dict[str, Any]:
    entries = {e.key: e for e in texts.entries()}
    keys = [k for k in dialog_manager.dialog_data.get(_KEYS, []) if k in entries]
    items = [
        {
            "id": str(number),
            "mark": "✏️" if entries[key].changed else "▫️",
            "snippet": entries[key].snippet(),
        }
        for number, key in enumerate(keys)
    ]
    return {
        "texts": items,
        "texts_count": len(items),
        "texts_mode": dialog_manager.dialog_data.get(_MODE, "search"),
        "texts_query": escape(dialog_manager.dialog_data.get(_QUERY, "")),
    }


@inject
async def text_view_getter(
    dialog_manager: DialogManager, texts: FromDishka[TextOverrides], **kwargs: Any
) -> dict[str, Any]:
    entry = texts.entry(dialog_manager.dialog_data[_KEY])
    return {
        "text_key": entry.key,
        "text_changed": int(entry.changed),
        "text_source": escape(entry.text),
        "text_variables": ", ".join(f"${v}" for v in sorted(entry.variables)) or "—",
    }


# ── handlers ─────────────────────────────────────────────────────────────────


async def _show_list(dialog_manager: DialogManager, keys: list[str], mode: str) -> None:
    dialog_manager.dialog_data[_KEYS] = keys
    dialog_manager.dialog_data[_MODE] = mode
    await dialog_manager.switch_to(SDashboard.TEXT_LIST)


@inject
async def on_text_search(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    texts: FromDishka[TextOverrides],
) -> None:
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    query = (message.text or "").strip()
    found = texts.search(query)
    if not found:
        await _say(notifier, _user(dialog_manager), "ntf-s-texts.not-found")
        return
    dialog_manager.dialog_data[_QUERY] = query
    await _show_list(dialog_manager, [e.key for e in found], "search")


@inject
async def on_texts_changed(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    texts: FromDishka[TextOverrides],
) -> None:
    changed = texts.changed()
    if not changed:
        await _say(notifier, _user(dialog_manager), "ntf-s-texts.none-changed")
        return
    await _show_list(dialog_manager, [e.key for e in changed], "changed")


async def on_text_select(
    callback: CallbackQuery, widget: Any, dialog_manager: DialogManager, item_id: str
) -> None:
    keys = dialog_manager.dialog_data.get(_KEYS, [])
    number = int(item_id)
    if not 0 <= number < len(keys):
        return
    dialog_manager.dialog_data[_KEY] = keys[number]
    dialog_manager.dialog_data.pop(_DRAFT, None)
    await dialog_manager.switch_to(SDashboard.TEXT_VIEW)


@inject
async def on_text_default(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    texts: FromDishka[TextOverrides],
) -> None:
    entry = texts.entry(dialog_manager.dialog_data[_KEY])
    if isinstance(callback.message, Message):
        await callback.message.answer(f"<pre>{escape(entry.default_text)}</pre>")
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND


@inject
async def on_text_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    texts: FromDishka[TextOverrides],
) -> None:
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    user = _user(dialog_manager)
    text = message.text or ""
    try:
        preview = await texts.check(dialog_manager.dialog_data[_KEY], text)
    except TextError as error:
        await _refuse(notifier, user, error)
        return
    try:
        await message.answer(preview)
    except TelegramBadRequest as error:
        await _say(notifier, user, "ntf-s-texts.telegram", detail=escape(error.message))
        return
    dialog_manager.dialog_data[_DRAFT] = text
    await dialog_manager.switch_to(SDashboard.TEXT_CONFIRM)


@inject
async def on_text_save(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    texts: FromDishka[TextOverrides],
) -> None:
    user = _user(dialog_manager)
    if not is_double_click(dialog_manager, key="s_text_save", cooldown=10):
        await notifier.notify_user(user, i18n_key="ntf-common.double-click-confirm")
        return
    try:
        await texts.save(user, dialog_manager.dialog_data[_KEY], dialog_manager.dialog_data[_DRAFT])
    except TextError as error:
        await _refuse(notifier, user, error)
        return
    except PermissionDeniedError:
        await notifier.notify_user(user, i18n_key="ntf-s-texts.denied")
        return
    dialog_manager.dialog_data.pop(_DRAFT, None)
    await notifier.notify_user(user, i18n_key="ntf-s-texts.saved")
    await dialog_manager.switch_to(SDashboard.TEXT_VIEW)


@inject
async def on_text_reset(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    texts: FromDishka[TextOverrides],
) -> None:
    user = _user(dialog_manager)
    if not is_double_click(dialog_manager, key="s_text_reset", cooldown=10):
        await notifier.notify_user(user, i18n_key="ntf-common.double-click-confirm")
        return
    try:
        await texts.reset(user, dialog_manager.dialog_data[_KEY])
    except PermissionDeniedError:
        await notifier.notify_user(user, i18n_key="ntf-s-texts.denied")
        return
    await notifier.notify_user(user, i18n_key="ntf-s-texts.reset")
