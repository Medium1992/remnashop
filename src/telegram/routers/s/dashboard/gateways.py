"""Where each payment gateway is offered: in the bot or in the web cabinet."""

from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from magic_filter import F

from src.application.common import TranslatorRunner
from src.application.common.dao import PaymentGatewayDao, SettingsDao
from src.application.dto import TelegramUserDto
from src.application.dto.s_settings import GatewayChannel
from src.application.use_cases.s.settings import ToggleGatewayChannel, ToggleGatewayChannelDto
from src.core.constants import USER_KEY
from src.core.enums import BannerName
from src.telegram.s_states import SDashboard
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Row, ScrollingGroup, Select, SwitchTo
from src.telegram.window import Window

_SEP = ":"


@inject
async def gateways_getter(
    dialog_manager: DialogManager,
    settings_dao: FromDishka[SettingsDao],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    i18n: FromDishka[TranslatorRunner],
    **kwargs: Any,
) -> dict[str, Any]:
    channels = (await settings_dao.get()).extra.s.channels
    items = []
    for gateway in await payment_gateway_dao.get_all():
        name = (gateway.settings and gateway.settings.display_name) or i18n.get(
            "gateway-type", gateway_type=gateway.type
        )
        for channel in GatewayChannel:
            items.append(
                {
                    "id": f"{gateway.type}{_SEP}{channel.value}",
                    "gateway": name,
                    "channel": channel.value,
                    "offered": int(channels.offers(gateway.type, channel)),
                    "active": int(gateway.is_active),
                }
            )
    return {"gateway_channels": items}


@inject
async def on_gateway_channel(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    item_id: str,
    toggle: FromDishka[ToggleGatewayChannel],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    gateway_type, _, channel = item_id.partition(_SEP)
    await toggle(user, ToggleGatewayChannelDto(gateway_type, GatewayChannel(channel)))


gateways = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.gateways"),
    ScrollingGroup(
        Select(
            I18nFormat(
                "btn-s.gateway-channel",
                gateway=F["item"]["gateway"],
                channel=F["item"]["channel"],
                offered=F["item"]["offered"],
                active=F["item"]["active"],
            ),
            id="gateway_channel",
            item_id_getter=lambda item: item["id"],
            items="gateway_channels",
            on_click=on_gateway_channel,
        ),
        id="gateway_channels",
        width=2,
        height=8,
        hide_on_single_page=True,
    ),
    Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=SDashboard.MAIN)),
    IgnoreUpdate(),
    state=SDashboard.GATEWAYS,
    getter=gateways_getter,
)
