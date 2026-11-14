"""A window whose text does not fit a photo caption goes without the photo.

The photo is dropped while the window renders: the dialog manager caches the
file id of the media in the message it is given, so dropped after that it would
cache an empty one and every later window with that banner would fail.
"""

from typing import Any

import pytest
from aiogram.types import Chat
from aiogram_dialog import Window as Base
from aiogram_dialog.api.entities import MediaAttachment, NewMessage

from src.telegram.window import Window, visible_length


def test_tags_and_entities_do_not_count() -> None:
    assert visible_length('<b>a</b> &lt;<a href="https://x">b</a>') == len("a <b")


def test_length_is_in_utf16_units() -> None:
    assert visible_length("💰") == 2


async def rendered(text: str, monkeypatch: pytest.MonkeyPatch) -> NewMessage:
    media = MediaAttachment(type="photo", path="banner.jpg")  # type: ignore[arg-type]

    async def render(self: Any, dialog: Any, manager: Any) -> NewMessage:
        return NewMessage(chat=Chat(id=1, type="private"), text=text, media=media)

    monkeypatch.setattr(Base, "render", render)
    window = Window.__new__(Window)
    window.state = "S:main"  # type: ignore[assignment]
    return await window.render(None, object())  # type: ignore[arg-type]


async def test_a_long_text_goes_without_the_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    message = await rendered("<b>" + "x" * 1025 + "</b>", monkeypatch)
    assert message.media is None and message.text == "<b>" + "x" * 1025 + "</b>"


async def test_a_text_that_fits_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (await rendered("<b>" + "x" * 1024 + "</b>", monkeypatch)).media is not None
