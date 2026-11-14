"""The "Connect" window of the "s" edition (docs/s/README.md).

Upstream's "Connect" opens the link right away, and copying it means a long
press on the button. With the window on, "Connect" opens the link as text to
copy, its QR code to scan from another device and the button that opens it.
"""

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from aiogram.enums import ButtonStyle, ContentType
from aiogram_dialog import Dialog, DialogManager
from aiogram_dialog.api.entities import MediaAttachment
from aiogram_dialog.widgets.media import DynamicMedia
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Format
from dishka import AsyncContainer
from dishka.integrations.aiogram import CONTAINER_NAME
from loguru import logger
from magic_filter import F

from src.application.common.dao import SettingsDao, SubscriptionDao
from src.application.dto import TelegramUserDto
from src.core.config import AppConfig
from src.core.constants import CONFIG_KEY, USER_KEY
from src.telegram.s_states import SConnect
from src.telegram.widgets import I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Cancel, CopyText, Row, Url, WebApp
from src.telegram.window import Window

QR_DIR = Path(tempfile.gettempdir()) / "remnashop-s-qr"


def qr_png(url: str) -> Path:
    """The QR code of ``url`` as a PNG, made once per link."""
    path = QR_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.png"
    if not path.exists():
        from qrcode import QRCode  # type: ignore[attr-defined]  # noqa: PLC0415
        from qrcode.constants import ERROR_CORRECT_M  # noqa: PLC0415

        qr: Any = QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(url)
        qr.make(fit=True)
        QR_DIR.mkdir(parents=True, exist_ok=True)
        # Written aside first: a half-written file is never sent.
        part = path.with_suffix(".part")
        qr.make_image(fill_color="black", back_color="white").save(part, format="PNG")
        part.replace(path)
    return path


async def connect_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    container: AsyncContainer = dialog_manager.middleware_data[CONTAINER_NAME]
    config: AppConfig = dialog_manager.middleware_data[CONFIG_KEY]
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    subscription = await (await container.get(SubscriptionDao)).get_current(user.id)
    if not subscription or not subscription.is_active or not subscription.url:
        return {"connectable": 0, "s_qr": None}

    settings = await (await container.get(SettingsDao)).get()
    qr = None
    try:
        qr = MediaAttachment(type=ContentType.PHOTO, path=str(qr_png(subscription.url)))
    except Exception as error:
        # The link and the buttons are still there without the picture.
        logger.warning(f"{user.log} QR code of the subscription link not made: {error}")

    return {
        "connectable": 1,
        "s_qr": qr,
        "subscription_url": subscription.url,
        "connection_url": config.bot.mini_app_url or subscription.url,
        "is_mini_app": config.bot.is_mini_app,
        "is_mini_app_reserve": config.bot.is_mini_app and settings.extra.mini_app_reserve,
    }


connect = Window(
    DynamicMedia("s_qr", when=F["s_qr"]),
    I18nFormat("msg-s-connect"),
    Row(
        CopyText(
            text=I18nFormat("btn-s-connect.copy"),
            copy_text=Format("{subscription_url}"),
        ),
        when=F["connectable"],
    ),
    Row(
        WebApp(
            text=I18nFormat("btn-s-connect.open"),
            url=Format("{connection_url}"),
            id="s_connect_miniapp",
            style=Style(ButtonStyle.PRIMARY),
        ),
        when=F["connectable"] & F["is_mini_app"],
    ),
    Row(
        Url(
            text=I18nFormat("btn-menu.connect-reserve"),
            url=Format("{subscription_url}"),
            id="s_connect_reserve",
        ),
        when=F["connectable"] & F["is_mini_app_reserve"],
    ),
    Row(
        Url(
            text=I18nFormat("btn-s-connect.open"),
            url=Format("{connection_url}"),
            id="s_connect_page",
            style=Style(ButtonStyle.PRIMARY),
        ),
        when=F["connectable"] & ~F["is_mini_app"],
    ),
    Row(Cancel(text=I18nFormat("btn-back.general"), id="s_connect_back")),
    IgnoreUpdate(),
    state=SConnect.MAIN,
    getter=connect_getter,
)

router = Dialog(connect)
