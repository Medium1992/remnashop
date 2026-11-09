from typing import Any, Optional

from aiogram.types import Message, TelegramObject
from loguru import logger

from src.core.enums import Deeplink


def _parse_deeplink_code(event: TelegramObject, deeplink: Deeplink) -> Optional[str]:
    if not isinstance(event, Message) or not event.text:
        return None

    parts = event.text.split()
    if len(parts) <= 1:
        return None

    code = parts[1]
    prefix = deeplink.with_underscore
    if code.startswith(prefix):
        raw = code[len(prefix) :]
        logger.debug(f"Parsed '{deeplink.value}' code '{raw}' from deeplink")
        return raw

    return None


def parse_referral_code(
    event: TelegramObject, data: Optional[dict[str, Any]] = None
) -> Optional[str]:
    # s edition: a one-time invite link is read as its owner's code, a dead
    # one as no code at all (middlewares/s_invites.py).
    if data is not None and "s_referral_code" in data:
        return data["s_referral_code"] or None
    return _parse_deeplink_code(event, Deeplink.REFERRAL)


def parse_ad_link_code(event: TelegramObject) -> Optional[str]:
    return _parse_deeplink_code(event, Deeplink.ADVERTISING)
