import html
import re
from dataclasses import dataclass
from typing import Optional

from aiogram_dialog import DialogManager, DialogProtocol
from aiogram_dialog import Window as WindowProtocol
from aiogram_dialog.api.entities import NewMessage
from loguru import logger

from src.core.constants import TEXT_MEDIA_MAX_LENGTH

_TAG_RE = re.compile(r"<[^>]*>")


def visible_length(text: str) -> int:
    """The length Telegram checks a caption against: no tags, in UTF-16 units."""
    return len(html.unescape(_TAG_RE.sub("", text)).encode("utf-16-le")) // 2


@dataclass
class NewMessageWithEffect(NewMessage):
    message_effect_id: Optional[str] = None


class Window(WindowProtocol):
    async def render(self, dialog: DialogProtocol, manager: DialogManager) -> NewMessage:
        new_message = await super().render(dialog, manager)

        # A text too long for a caption goes without the picture (a text may be
        # 4096 long), else Telegram refuses the window: "caption is too long".
        # Dropped here, before the dialog manager sees the message: dropped
        # later, the manager caches an empty file id for the picture.
        media = new_message.media
        if media and visible_length(new_message.text or "") > TEXT_MEDIA_MAX_LENGTH:
            logger.debug(f"Text too long for a caption, '{self.get_state()}' goes without media")
            media = None

        return NewMessageWithEffect(
            chat=new_message.chat,
            thread_id=new_message.thread_id,
            business_connection_id=new_message.business_connection_id,
            text=new_message.text,
            reply_markup=new_message.reply_markup,
            parse_mode=new_message.parse_mode,
            show_mode=new_message.show_mode,
            media=media,
            link_preview_options=new_message.link_preview_options,
            message_effect_id=getattr(manager, "message_effect_id", None),
        )
