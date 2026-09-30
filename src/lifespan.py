import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from adaptix import Retort
from aiogram import Dispatcher
from aiogram.types import WebhookInfo
from dishka import AsyncContainer, Scope
from fastapi import FastAPI
from loguru import logger
from redis.asyncio import Redis

from src.application.common import BotService, Remnawave
from src.application.common.dao import SettingsDao
from src.application.events import (
    BotInlineModeDisabledEvent,
    BotShutdownEvent,
    BotStartupEvent,
    RemnawaveErrorEvent,
    RemnawaveVersionWarningEvent,
    WebhookErrorEvent,
)
from src.application.events.system import RemnashopWelcomeEvent
from src.application.use_cases.gateways.commands.payment import CreateDefaultPaymentGateway
from src.application.use_cases.remnawave.commands.identity import BackfillRemnaIdentity
from src.application.use_cases.settings.commands.defaults import CreateDefaultSettings
from src.core.config import AppConfig
from src.core.panel import is_ahead_of_known
from src.core.utils.i18n_helpers import i18n_format_seconds
from src.core.utils.time import get_uptime
from src.infrastructure.redis.keys import WelcomedVersionKey
from src.infrastructure.services import (
    CommandService,
    EventBusImpl,
    NotificationWorker,
    WebhookService,
)
from src.web.endpoints import TelegramWebhookEndpoint


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    dispatcher: Dispatcher = app.state.dispatcher
    telegram_webhook_endpoint: TelegramWebhookEndpoint = app.state.telegram_webhook_endpoint
    container: AsyncContainer = app.state.dishka_container

    event_bus = await container.get(EventBusImpl)
    event_bus.set_container_factory(lambda: container)
    event_bus.autodiscover()

    notification_worker = await container.get(NotificationWorker)
    notification_worker.set_container_factory(lambda: container)

    async with container(scope=Scope.REQUEST) as startup_container:
        config = await startup_container.get(AppConfig)
        bot_service = await startup_container.get(BotService)
        settings_dao = await startup_container.get(SettingsDao)
        webhook_service = await startup_container.get(WebhookService)
        command_service = await startup_container.get(CommandService)
        create_default_payment_gateway = await startup_container.get(CreateDefaultPaymentGateway)
        create_default_settings = await startup_container.get(CreateDefaultSettings)
        redis = await startup_container.get(Redis)
        retort = await startup_container.get(Retort)

        if not await bot_service.is_inline_enabled():
            logger.warning(
                "Bot is not enabled for inline mode. "
                "Please enable Inline Mode in BotFather for correct work of some features"
            )
            await event_bus.publish(BotInlineModeDisabledEvent())

        states = await bot_service.get_bot_states()
        await create_default_payment_gateway.system()
        # Seed the default settings row (committed, cache-invalidating) before any
        # request can read it, so `get()` never has to lazily create — and cache —
        # an uncommitted row whose id would later fail to match on update().
        await create_default_settings.system()
        settings = await settings_dao.get()
        allowed_updates = dispatcher.resolve_used_update_types()
        webhook_info: WebhookInfo = await webhook_service.setup_webhook(allowed_updates)

        if webhook_service.has_error(webhook_info):
            logger.critical(
                f"Webhook has a last error message: '{webhook_info.last_error_message}'"
            )
            webhook_error_event = WebhookErrorEvent(
                error_message=webhook_info.last_error_message,
            )
            await event_bus.publish(webhook_error_event)

        await command_service.setup_commands()

    await telegram_webhook_endpoint.startup()

    logger.opt(colors=True).info(
        rf"""
    <cyan> _____                                _                 </>
    <cyan>|  __ \                              | |                </>
    <cyan>| |__) |___ _ __ ___  _ __   __ _ ___| |__   ___  _ __  </>
    <cyan>|  _  // _ \ '_ ` _ \| '_ \ / _` / __| '_ \ / _ \| '_ \ </>
    <cyan>| | \ \  __/ | | | | | | | | (_| \__ \ | | | (_) | |_) |</>
    <cyan>|_|  \_\___|_| |_| |_|_| |_|\__,_|___/_| |_|\___/| .__/ </>
    <cyan>                                                 | |    </>
    <cyan>                                                 |_|    </>

        <green>Build Time: {config.build.time}</>
        <green>Branch: {config.build.branch} ({config.build.tag})</>
        <green>Commit: {config.build.commit}</>
        <cyan>------------------------</>
        Groups Mode  - {states["groups_mode"]}"
        Privacy Mode - {states["privacy_mode"]}"
        Inline Mode  - {states["inline_mode"]}"
        <cyan>------------------------</>
        <yellow>Bot in access mode: '{settings.access.mode}'</>
        <yellow>Payments allowed: '{settings.access.payments_allowed}'</>
        <yellow>Registration allowed: '{settings.access.registration_allowed}'</>
        """  # noqa: W605
    )

    current_version = config.build.tag or "dev"
    welcomed_key = retort.dump(WelcomedVersionKey(version="*"))
    last_welcomed = await redis.get(welcomed_key)

    if last_welcomed != current_version:
        await redis.set(welcomed_key, current_version)
        await event_bus.publish(RemnashopWelcomeEvent())

    bot_startup_event = BotStartupEvent(
        **config.build.data,
        access_mode=settings.access.mode,
        payments_allowed=settings.access.payments_allowed,
        registration_allowed=settings.access.registration_allowed,
    )
    await event_bus.publish(bot_startup_event)

    panel_retry: Optional[asyncio.Task[None]] = None
    try:
        await connect_panel(container, config, event_bus)
    except Exception as e:
        remnawave_error_event = RemnawaveErrorEvent(**config.build.data, exception=e)
        await event_bus.publish(remnawave_error_event)
        # Typical right after a panel upgrade: the panel is still running its
        # own migrations when the bot comes up. Keep trying in the background,
        # so the identity backfill still happens without anyone restarting us.
        panel_retry = asyncio.create_task(retry_panel(container, config, event_bus))

    yield

    if panel_retry is not None:
        panel_retry.cancel()

    bot_shutdown_event = BotShutdownEvent(
        **config.build.data,
        uptime=i18n_format_seconds(get_uptime()),
    )
    await event_bus.publish(bot_shutdown_event)

    await asyncio.sleep(2)

    await event_bus.shutdown()
    await notification_worker.shutdown()
    await telegram_webhook_endpoint.shutdown()
    await command_service.delete_commands()
    await webhook_service.delete_webhook()
    await container.close()


# Between attempts once the panel was unreachable at startup.
PANEL_RETRY_INTERVAL = 30.0


async def connect_panel(
    container: AsyncContainer,
    config: AppConfig,
    event_bus: EventBusImpl,
) -> None:
    """Handshake with the panel, then re-key any rows an older build left behind.

    Runs in a scope of its own: the backfill writes, and it needs a session that
    is still open and a transaction it can commit.
    """
    async with container(scope=Scope.REQUEST) as scope:
        remnawave = await scope.get(Remnawave)
        backfill = await scope.get(BackfillRemnaIdentity)

        panel_version = await remnawave.try_connection()

        # Only meaningful once the panel line is known: on 3.x a legacy row is
        # resolved through the shortUuid in its stored url, on 2.x through the
        # uuid it already holds.
        await backfill.run()

    if is_ahead_of_known(panel_version):
        await event_bus.publish(
            RemnawaveVersionWarningEvent(
                **config.build.data,
                panel_version=str(panel_version),
            )
        )


async def retry_panel(
    container: AsyncContainer,
    config: AppConfig,
    event_bus: EventBusImpl,
    interval: float = PANEL_RETRY_INTERVAL,
) -> None:
    """Keep trying :func:`connect_panel` until it succeeds or the app stops."""
    while True:
        await asyncio.sleep(interval)
        try:
            await connect_panel(container, config, event_bus)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Remnawave panel still unreachable, retrying in {interval:.0f}s: {e}")
            continue

        logger.info("Remnawave panel reached after startup; identity backfill done")
        return
