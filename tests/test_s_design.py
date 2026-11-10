"""Newer Bot API presentation: dates in the reader's format, a disabled button."""

from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram.types import InlineKeyboardMarkup
from aiogram_dialog.widgets.text import Const

from src.core.utils.s_time import DATE, DATE_TIME, tg_time
from src.telegram.widgets.s_disabled import SDisabled

MOMENT = datetime(2027, 1, 31, 15, 30, tzinfo=timezone.utc)


class TestTgTime:
    def test_the_tag_carries_the_moment_and_the_fallback(self) -> None:
        assert tg_time(MOMENT, DATE_TIME, "31.01.2027 15:30 UTC") == (
            '<tg-time unix="1801409400" format="Dt">31.01.2027 15:30 UTC</tg-time>'
        )

    @pytest.mark.parametrize("fmt", [DATE, DATE_TIME, "r", "wDT", "t", ""])
    def test_formats_telegram_accepts(self, fmt: str) -> None:
        assert f'format="{fmt}"' in tg_time(MOMENT, fmt, "x")

    @pytest.mark.parametrize("fmt", ["rD", "Tt", "x", "DD"])
    def test_formats_telegram_rejects_fail_here(self, fmt: str) -> None:
        with pytest.raises(ValueError):
            tg_time(MOMENT, fmt, "x")

    def test_the_fallback_is_escaped(self) -> None:
        assert ">a &lt;b&gt;</tg-time>" in tg_time(MOMENT, DATE, "a <b>")


class TestDisabledButton:
    async def test_it_renders_as_a_disabled_inline_button(self) -> None:
        keyboard: Any = await SDisabled(Const("🔗 3 из 3"), id="limit").render_keyboard(
            {},
            None,  # type: ignore[arg-type]
        )

        button = keyboard[0][0]
        assert button.text == "🔗 3 из 3"
        assert button.callback_data is None
        sent = InlineKeyboardMarkup(inline_keyboard=keyboard).model_dump(exclude_none=True)
        assert sent["inline_keyboard"][0][0] == {"text": "🔗 3 из 3", "disabled": {}}

    async def test_it_hides_by_its_condition(self) -> None:
        widget = SDisabled(Const("x"), id="limit", when=lambda data, w, m: False)
        assert await widget.render_keyboard({}, None) == []  # type: ignore[arg-type]
