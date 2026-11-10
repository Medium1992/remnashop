from aiogram.enums import ButtonStyle
from aiogram_dialog import Dialog, StartMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Format
from magic_filter import F

from src.core.enums import BannerName
from src.telegram.s_states import SDashboard
from src.telegram.states import DashboardRemnashop
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import (
    Button,
    Column,
    Row,
    ScrollingGroup,
    Select,
    Start,
    SwitchTo,
)
from src.telegram.window import Window

from .getters import blocked_getter, inactive_list_getter, s_getter
from .handlers import (
    on_back_from_number,
    on_back_to_kind,
    on_channel_check,
    on_channel_input,
    on_channel_reset,
    on_channel_select,
    on_channel_toggle,
    on_inactive_select,
    on_number_input,
    on_number_request,
    on_preview_request,
    on_refresh_all,
    on_routers_input,
    on_send_confirm,
    on_send_request,
    on_summary,
    on_target_input,
    on_toggle,
)


def _toggle(widget_id: str) -> Button:
    return Button(
        I18nFormat(f"btn-s.{widget_id.replace('_', '-')}", enabled=F[widget_id]),
        id=widget_id,
        on_click=on_toggle,
    )


def _number(widget_id: str) -> Button:
    return Button(
        I18nFormat(f"btn-s.{widget_id.replace('_', '-')}", value=F[widget_id]),
        id=widget_id,
        on_click=on_number_request,
    )


def _back(state: object = SDashboard.MAIN) -> Row:
    return Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=state))  # type: ignore[arg-type]


def _reminder_actions() -> list[Row]:
    return [
        Row(Button(I18nFormat("btn-s.summary"), id="summary", on_click=on_summary)),
        Row(Button(I18nFormat("btn-s.preview"), id="preview", on_click=on_preview_request)),
        Row(Button(I18nFormat("btn-s.send-now"), id="send", on_click=on_send_request)),
    ]


main = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.main"),
    Row(
        SwitchTo(
            I18nFormat("btn-s.slots", enabled=F["slots_enabled"]),
            id="slots",
            state=SDashboard.SLOTS,
        )
    ),
    Row(
        SwitchTo(
            I18nFormat("btn-s.versions", enabled=F["versions_enabled"]),
            id="versions",
            state=SDashboard.VERSIONS,
        )
    ),
    Row(
        SwitchTo(
            I18nFormat("btn-s.stale", enabled=F["stale_enabled"]),
            id="stale",
            state=SDashboard.STALE,
        )
    ),
    Row(
        SwitchTo(
            I18nFormat("btn-s.inactive", enabled=F["inactive_enabled"]),
            id="inactive",
            state=SDashboard.INACTIVE,
        )
    ),
    Row(
        SwitchTo(
            I18nFormat("btn-s.invites", enabled=F["invites_enabled"]),
            id="invites",
            state=SDashboard.INVITES,
        )
    ),
    Row(_toggle("plans_calendar")),
    Row(SwitchTo(I18nFormat("btn-s.blocked"), id="blocked", state=SDashboard.BLOCKED)),
    Row(
        SwitchTo(
            I18nFormat("btn-s.routers", value=F["routers_count"]),
            id="routers",
            state=SDashboard.ROUTERS,
        )
    ),
    Row(
        Start(
            I18nFormat("btn-back.general"),
            id="back",
            state=DashboardRemnashop.MAIN,
            mode=StartMode.RESET_STACK,
        )
    ),
    IgnoreUpdate(),
    state=SDashboard.MAIN,
    getter=s_getter,
)

slots = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.slots"),
    Row(_toggle("slots_enabled")),
    Row(_number("slots_base"), _number("slots_ttl")),
    _back(),
    IgnoreUpdate(),
    state=SDashboard.SLOTS,
    getter=s_getter,
)

versions = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.versions"),
    Row(_toggle("versions_enabled"), _toggle("versions_notify")),
    Row(_number("versions_interval"), _number("versions_refresh")),
    Row(_toggle("versions_ignore_patch"), _number("versions_min_devices")),
    Column(
        Select(
            Format("{item[icon]} {item[title]}: {item[value]}"),
            id="channels",
            item_id_getter=lambda item: item["id"],
            items="channels",
            on_click=on_channel_select,
        ),
    ),
    Row(Button(I18nFormat("btn-s.refresh-all"), id="refresh_all", on_click=on_refresh_all)),
    *_reminder_actions(),
    _back(),
    IgnoreUpdate(),
    state=SDashboard.VERSIONS,
    getter=s_getter,
)

channel = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.channel"),
    Row(
        Button(
            I18nFormat("btn-s.channel-toggle", enabled=F["ch_enabled"]),
            id="channel_toggle",
            on_click=on_channel_toggle,
        ),
        Button(I18nFormat("btn-s.channel-check"), id="channel_check", on_click=on_channel_check),
    ),
    Row(
        Button(
            I18nFormat("btn-s.channel-reset"),
            id="channel_reset",
            on_click=on_channel_reset,
            style=Style(ButtonStyle.DANGER),
        )
    ),
    _back(SDashboard.VERSIONS),
    MessageInput(func=on_channel_input),
    IgnoreUpdate(),
    state=SDashboard.CHANNEL,
    getter=s_getter,
)

stale = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.stale"),
    Row(_toggle("stale_enabled")),
    Row(_number("stale_hours"), _number("stale_interval")),
    *_reminder_actions(),
    _back(),
    IgnoreUpdate(),
    state=SDashboard.STALE,
    getter=s_getter,
)

inactive = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.inactive"),
    Row(_toggle("inactive_enabled")),
    Row(_number("inactive_days"), _number("inactive_warn")),
    Row(_toggle("inactive_include_disabled")),
    Row(
        SwitchTo(
            I18nFormat("btn-s.inactive-list"), id="inactive_list", state=SDashboard.INACTIVE_LIST
        )
    ),
    _back(),
    IgnoreUpdate(),
    state=SDashboard.INACTIVE,
    getter=s_getter,
)

invites = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.invites"),
    Row(_toggle("invites_enabled")),
    Row(_number("invites_ttl"), _number("invites_max")),
    _back(),
    IgnoreUpdate(),
    state=SDashboard.INVITES,
    getter=s_getter,
)

inactive_list = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.inactive-list"),
    ScrollingGroup(
        Select(
            I18nFormat(
                "btn-s.inactive-item",
                name=F["item"]["name"],
                status=F["item"]["status"],
                since=F["item"]["since"],
                online=F["item"]["online"],
            ),
            id="inactive_select",
            item_id_getter=lambda item: item["user_id"],
            items="inactive",
            on_click=on_inactive_select,
        ),
        id="inactive_scroll",
        width=1,
        height=8,
        hide_on_single_page=True,
    ),
    _back(SDashboard.INACTIVE),
    IgnoreUpdate(),
    state=SDashboard.INACTIVE_LIST,
    getter=inactive_list_getter,
)

blocked = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.blocked"),
    ScrollingGroup(
        Select(
            I18nFormat("btn-s.blocked-item", name=F["item"]["name"], until=F["item"]["until"]),
            id="blocked_select",
            item_id_getter=lambda item: item["user_id"],
            items="blocked",
            on_click=on_inactive_select,
        ),
        id="blocked_scroll",
        width=1,
        height=8,
        hide_on_single_page=True,
    ),
    _back(),
    IgnoreUpdate(),
    state=SDashboard.BLOCKED,
    getter=blocked_getter,
)

routers = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.routers"),
    _back(),
    MessageInput(func=on_routers_input),
    IgnoreUpdate(),
    state=SDashboard.ROUTERS,
    getter=s_getter,
)

number = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.number"),
    Row(Button(I18nFormat("btn-back.general"), id="back", on_click=on_back_from_number)),
    MessageInput(func=on_number_input),
    IgnoreUpdate(),
    state=SDashboard.NUMBER,
    getter=s_getter,
)

target = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.target"),
    Row(Button(I18nFormat("btn-back.general"), id="back", on_click=on_back_to_kind)),
    MessageInput(func=on_target_input),
    IgnoreUpdate(),
    state=SDashboard.TARGET,
    getter=s_getter,
)

confirm_send = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.confirm-send"),
    Row(
        Button(
            I18nFormat("btn-s.send-confirm"),
            id="send_confirm",
            on_click=on_send_confirm,
            style=Style(ButtonStyle.SUCCESS),
        )
    ),
    Row(Button(I18nFormat("btn-back.general"), id="back", on_click=on_back_to_kind)),
    IgnoreUpdate(),
    state=SDashboard.CONFIRM_SEND,
    getter=s_getter,
)

router = Dialog(
    main,
    slots,
    versions,
    channel,
    stale,
    inactive,
    inactive_list,
    invites,
    blocked,
    routers,
    number,
    target,
    confirm_send,
)
