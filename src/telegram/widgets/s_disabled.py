"""A button that is shown but does nothing (Bot API 10.3, ``disabled``).

aiogram-dialog has no widget for it yet. Used where hiding a button would
leave the user wondering where it went: the limit it ran into is on the face.
"""

from aiogram.types import DisabledButton, InlineKeyboardButton
from aiogram_dialog.api.internal import RawKeyboard
from aiogram_dialog.api.protocols import DialogManager
from aiogram_dialog.widgets.common import WhenCondition
from aiogram_dialog.widgets.kbd import Keyboard
from aiogram_dialog.widgets.text import Text


class SDisabled(Keyboard):
    def __init__(self, text: Text, id: str, when: WhenCondition = None) -> None:
        super().__init__(id=id, when=when)
        self.text = text

    async def _render_keyboard(self, data: dict, manager: DialogManager) -> RawKeyboard:
        text = await self.text.render_text(data, manager)
        return [[InlineKeyboardButton(text=text, disabled=DisabledButton())]]
