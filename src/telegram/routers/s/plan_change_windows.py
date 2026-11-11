"""Windows of the plan change, added to upstream's subscription dialog."""

from aiogram.enums import ButtonStyle
from aiogram_dialog import Window
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Format
from magic_filter import F

from src.core.constants import PAYMENT_PREFIX
from src.core.enums import BannerName
from src.telegram.keyboards import back_main_menu_button
from src.telegram.states import Subscription
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Button, Column, Multiselect, Row, SwitchTo

from .plan_change import (
    DEVICES_WIDGET,
    change_devices_getter,
    change_mode_getter,
    on_change_devices_done,
    on_change_mode,
)

_back_duration = Row(
    SwitchTo(
        text=I18nFormat("btn-subscription.back-duration"),
        id=f"{PAYMENT_PREFIX}s_change_back",
        state=Subscription.DURATION,
    ),
)

change_mode = Window(
    Banner(BannerName.SUBSCRIPTION),
    I18nFormat("msg-s-change.mode"),
    Column(
        Button(
            text=I18nFormat(
                "btn-s-change.keep",
                date=F["s_keep_date"],
                amount=F["s_keep_amount"],
                free=F["s_keep_free"],
                currency=F["s_currency"],
            ),
            id="s_change_keep",
            on_click=on_change_mode,
            when=F["s_keep"],
        ),
        Button(
            text=I18nFormat(
                "btn-s-change.today",
                period=F["s_today_period"],
                amount=F["s_today_amount"],
                free=F["s_today_free"],
                currency=F["s_currency"],
            ),
            id="s_change_today",
            on_click=on_change_mode,
            when=F["s_today"],
        ),
    ),
    _back_duration,
    *back_main_menu_button,
    IgnoreUpdate(),
    state=Subscription.S_CHANGE_MODE,
    getter=change_mode_getter,
)

change_devices = Window(
    Banner(BannerName.SUBSCRIPTION),
    I18nFormat("msg-s-change.devices"),
    Column(
        Multiselect(
            checked_text=Format("❌ {item[title]}"),
            unchecked_text=Format("📱 {item[title]}"),
            id=DEVICES_WIDGET,
            item_id_getter=lambda item: item["id"],
            items="s_devices",
        ),
    ),
    Row(
        Button(
            text=I18nFormat("btn-s-change.devices-done", checked=F["s_checked"], must=F["s_must"]),
            id="s_change_devices_done",
            on_click=on_change_devices_done,
            style=Style(ButtonStyle.SUCCESS),
        ),
    ),
    _back_duration,
    *back_main_menu_button,
    IgnoreUpdate(),
    state=Subscription.S_CHANGE_DEVICES,
    getter=change_devices_getter,
)
