from aiogram_dialog import Dialog, StartMode, Window
from aiogram_dialog.widgets.input import MessageInput
from magic_filter import F

from src.application.common.policy import Permission
from src.core.enums import AccessMode, BannerName
from src.telegram.keyboards import main_menu_button
from src.telegram.states import Dashboard, DashboardAccess
from src.telegram.utils import require_permission
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Button, Column, Group, Row, Select, Start, SwitchTo

from .getters import access_getter, channel_getter, conditions_getter, rules_getter
from .handlers import (
    on_access_mode_select,
    on_channel_input,
    on_condition_toggle,
    on_payments_toggle,
    on_registration_toggle,
    on_rules_input,
)

access = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-access-main"),
    Row(
        SwitchTo(
            text=I18nFormat("btn-access.conditions"),
            id="conditions",
            state=DashboardAccess.CONDITIONS,
        ),
    ),
    Row(
        Button(
            text=I18nFormat("btn-access.payments-toggle", enabled=F["payments_allowed"]),
            id="payments",
            on_click=on_payments_toggle,
        ),
        Button(
            text=I18nFormat("btn-access.registration-toggle", enabled=F["registration_allowed"]),
            id="registration",
            on_click=on_registration_toggle,
        ),
    ),
    Column(
        Select(
            text=I18nFormat("btn-access.mode", access_mode=F["item"]),
            id="mode",
            item_id_getter=lambda item: item.value,
            items="modes",
            type_factory=AccessMode,
            on_click=on_access_mode_select,
        ),
    ),
    Row(
        Start(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=Dashboard.MAIN,
            mode=StartMode.RESET_STACK,
        ),
        *main_menu_button,
    ),
    IgnoreUpdate(),
    state=DashboardAccess.MAIN,
    getter=access_getter,
)

conditions = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-access-conditions"),
    Group(
        SwitchTo(
            text=I18nFormat("btn-access.rules"),
            id="rules_edit",
            state=DashboardAccess.RULES,
            when=require_permission(Permission.SETTINGS_REQUIREMENT),
        ),
        Button(
            text=I18nFormat("btn-access.condition-toggle", enabled=F["rules"]),
            id="rules",
            on_click=on_condition_toggle,
            when=require_permission(Permission.SETTINGS_REQUIREMENT),
        ),
        SwitchTo(
            text=I18nFormat("btn-access.channel"),
            id="channel_edit",
            state=DashboardAccess.CHANNEL,
            when=require_permission(Permission.SETTINGS_REQUIREMENT),
        ),
        Button(
            text=I18nFormat("btn-access.condition-toggle", enabled=F["channel"]),
            id="channel",
            on_click=on_condition_toggle,
            when=require_permission(Permission.SETTINGS_REQUIREMENT),
        ),
        width=2,
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=DashboardAccess.MAIN,
        ),
    ),
    IgnoreUpdate(),
    state=DashboardAccess.CONDITIONS,
    getter=conditions_getter,
)

rules = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-access-rules"),
    I18nFormat("msg-s-rules.admin"),
    Row(
        SwitchTo(
            I18nFormat("btn-back.general"),
            id="back",
            state=DashboardAccess.CONDITIONS,
        ),
    ),
    MessageInput(func=on_rules_input),
    IgnoreUpdate(),
    state=DashboardAccess.RULES,
    getter=rules_getter,
)

channel = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-access-channel"),
    Row(
        SwitchTo(
            I18nFormat("btn-back.general"),
            id="back",
            state=DashboardAccess.CONDITIONS,
        ),
    ),
    MessageInput(func=on_channel_input),
    IgnoreUpdate(),
    state=DashboardAccess.CHANNEL,
    getter=channel_getter,
)

router = Dialog(
    access,
    conditions,
    rules,
    channel,
)
