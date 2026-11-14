import html
import re
from dataclasses import replace
from typing import Optional

from aiogram import Bot
from aiogram.types import Message
from aiogram_dialog.api.entities import NewMessage, OldMessage
from aiogram_dialog.manager.message_manager import SEND_METHODS
from aiogram_dialog.manager.message_manager import MessageManager as MessageManagerProtocol
from loguru import logger

from src.core.constants import TEXT_MEDIA_MAX_LENGTH

_TAG_RE = re.compile(r"<[^>]*>")


def visible_length(text: str) -> int:
    """The length Telegram checks a caption against: no tags, in UTF-16 units."""
    return len(html.unescape(_TAG_RE.sub("", text)).encode("utf-16-le")) // 2


class MessageManager(MessageManagerProtocol):
    async def show_message(
        self,
        bot: Bot,
        new_message: NewMessage,
        old_message: Optional[OldMessage],
    ) -> OldMessage:
        # A text too long for a caption goes without the picture (a text may be
        # 4096 long), else Telegram refuses the window: "caption is too long".
        if new_message.media and visible_length(new_message.text or "") > TEXT_MEDIA_MAX_LENGTH:
            logger.debug("Text too long for a caption, the window goes without its media")
            new_message = replace(new_message, media=None)
        return await super().show_message(bot, new_message, old_message)

    async def send_text(self, bot: Bot, new_message: NewMessage) -> Message:
        return await bot.send_message(
            new_message.chat.id,
            text=new_message.text,  # type: ignore[arg-type]
            message_thread_id=new_message.thread_id,
            business_connection_id=new_message.business_connection_id,
            reply_markup=new_message.reply_markup,
            parse_mode=new_message.parse_mode,
            link_preview_options=new_message.link_preview_options,
            message_effect_id=getattr(new_message, "message_effect_id", None),
        )

    async def send_media(self, bot: Bot, new_message: NewMessage) -> Message:
        method = getattr(bot, SEND_METHODS[new_message.media.type], None)  # type: ignore[union-attr]

        if not method:
            raise ValueError(
                f"ContentType {new_message.media.type} is not supported",  # type: ignore[union-attr]
            )

        return await method(  # type: ignore[no-any-return]
            new_message.chat.id,
            await self.get_media_source(new_message.media, bot),  # type: ignore[arg-type]
            message_thread_id=new_message.thread_id,
            business_connection_id=new_message.business_connection_id,
            caption=new_message.text,
            reply_markup=new_message.reply_markup,
            parse_mode=new_message.parse_mode,
            message_effect_id=getattr(new_message, "message_effect_id", None),
            **new_message.media.kwargs,  # type: ignore[union-attr]
        )
