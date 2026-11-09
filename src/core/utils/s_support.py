"""Support link override of the "s" edition.

Upstream builds every "contact support" link from ``BOT_SUPPORT_USERNAME``
as ``t.me/<username>?text=...``, so it can only point at a person. With
``S_SUPPORT_URL`` set, every such button opens that URL as is instead — for
example a channel's direct messages (``https://t.me/<channel>?direct``) or a
support group. Nothing is appended: the prefilled ``?text=`` would break a URL
that already has a query, and a channel chat ignores it anyway.
"""

import os

from loguru import logger

from src.core.utils.validators import is_valid_url

SUPPORT_URL_ENV = "S_SUPPORT_URL"


def support_url_override() -> str:
    value = os.environ.get(SUPPORT_URL_ENV, "").strip()
    if value and not is_valid_url(value):
        logger.warning(
            f"{SUPPORT_URL_ENV} is not a valid URL, falling back to BOT_SUPPORT_USERNAME"
        )
        return ""
    return value
