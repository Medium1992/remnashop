"""Where each payment gateway is offered (docs/s/README.md).

Telegram takes digital goods in bots and Mini Apps only for Stars. So the bot,
and the cabinet when it is open as the bot's Mini App, offer the gateways of
the "bot" channel, Stars alone by default; the cabinet in a browser offers the
"web" channel, every gateway but Stars by default (Stars are paid only inside
Telegram). An admin changes both lists in the S section.
"""

from loguru import logger

from src.application.common import Notifier
from src.application.common.dao import PaymentGatewayDao, SettingsDao
from src.application.common.uow import UnitOfWork
from src.application.dto import MessagePayloadDto, PaymentGatewayDto
from src.application.dto.s_settings import GatewayChannel
from src.core.enums import PaymentGatewayType, Role

Channel = GatewayChannel


class GatewayChannels:
    def __init__(
        self,
        uow: UnitOfWork,
        payment_gateway_dao: PaymentGatewayDao,
        settings_dao: SettingsDao,
        notifier: Notifier,
    ) -> None:
        self.uow = uow
        self.payment_gateway_dao = payment_gateway_dao
        self.settings_dao = settings_dao
        self.notifier = notifier

    async def active(self, channel: GatewayChannel) -> list[PaymentGatewayDto]:
        """Active gateways offered in the channel, in the admin's order."""
        channels = (await self.settings_dao.get()).extra.s.channels
        gateways = await self.payment_gateway_dao.get_active()
        return [g for g in gateways if channels.offers(g.type, channel)]

    async def for_bot(self) -> list[PaymentGatewayDto]:
        return await self.active(GatewayChannel.BOT)

    async def allows(self, gateway_type: PaymentGatewayType, channel: GatewayChannel) -> bool:
        return (await self.settings_dao.get()).extra.s.channels.offers(gateway_type, channel)

    async def tell_once(self) -> None:
        """Tell the owner, once, which gateways the default took out of the bot.

        Nothing is said when the bot loses nothing, or once an admin chose the
        bot's gateways themselves.
        """
        async with self.uow:
            settings = await self.settings_dao.get()
            channels = settings.extra.s.channels
            if channels.notified:
                return
            gateways = await self.payment_gateway_dao.get_active()
            hidden = [g.type for g in gateways if not channels.offers(g.type, GatewayChannel.BOT)]
            channels.notified = True
            await self.settings_dao.update(settings)
            await self.uow.commit()

        if not hidden:
            return
        logger.info(f"Gateways not offered in the bot: {', '.join(hidden)}")
        try:
            await self.notifier.notify_admins(
                MessagePayloadDto(
                    i18n_key="event-s-channels.bot-stars-only",
                    i18n_kwargs={"gateways": ", ".join(hidden)},
                    delete_after=None,
                ),
                roles=[Role.OWNER, Role.DEV],
            )
        except Exception:
            logger.exception("The owner was not told about the bot's gateways")
