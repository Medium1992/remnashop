"""A window whose text does not fit a photo caption goes without the photo."""

from typing import Any

import pytest
from aiogram.types import Chat
from aiogram_dialog.api.entities import MediaAttachment, NewMessage
from aiogram_dialog.manager.message_manager import MessageManager as Base

from src.telegram.message_manager import MessageManager, visible_length


def test_tags_and_entities_do_not_count() -> None:
    assert visible_length('<b>a</b> &lt;<a href="https://x">b</a>') == len("a <b")


def test_length_is_in_utf16_units() -> None:
    assert visible_length("💰") == 2


async def shown(text: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    seen: list[NewMessage] = []

    async def show(self: Any, bot: Any, new_message: NewMessage, old_message: Any) -> Any:
        seen.append(new_message)

    monkeypatch.setattr(Base, "show_message", show)
    media = MediaAttachment(type="photo", path="banner.jpg")  # type: ignore[arg-type]
    message = NewMessage(chat=Chat(id=1, type="private"), text=text, media=media)
    await MessageManager().show_message(None, message, None)  # type: ignore[arg-type]
    return seen[0].media


async def test_a_long_text_goes_without_the_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await shown("<b>" + "x" * 1025 + "</b>", monkeypatch) is None


async def test_a_text_that_fits_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await shown("<b>" + "x" * 1024 + "</b>", monkeypatch) is not None
