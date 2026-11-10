"""Dates shown by Telegram in the reader's own way (Bot API 9.5, ``date_time``).

``<tg-time>`` carries the moment itself; the client renders it in the user's
language and date format. The text inside is what a client too old for the
entity shows instead, so it stays a complete date on its own.
"""

import re
from datetime import datetime
from html import escape
from typing import Final

# Telegram's format string: "r" (relative), or weekday / date / time parts.
_FORMAT: Final[re.Pattern[str]] = re.compile(r"r|w?[dD]?[tT]?")

DATE: Final[str] = "D"  # long date
DATE_TIME: Final[str] = "Dt"  # long date, short time


def tg_time(moment: datetime, fmt: str, fallback: str) -> str:
    """HTML for ``moment`` in the reader's format; ``fallback`` for old clients."""
    if not _FORMAT.fullmatch(fmt):
        raise ValueError(f"Invalid date_time format '{fmt}'")
    return f'<tg-time unix="{int(moment.timestamp())}" format="{fmt}">{escape(fallback)}</tg-time>'
