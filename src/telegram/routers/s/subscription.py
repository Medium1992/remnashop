"""The "s" addition to the subscription screen: when a lapsed profile goes."""

from datetime import timedelta
from typing import Any, Optional

from aiogram_dialog import DialogManager
from dishka import AsyncContainer
from dishka.integrations.aiogram import CONTAINER_NAME

from src.application.common import TranslatorRunner
from src.application.common.dao import SettingsDao
from src.application.dto import SubscriptionDto
from src.core.enums import SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.services.s.blocks import BlockService
from src.infrastructure.services.s.inactive import DATE_FORMAT, inactive_since

from .devices import block_banner


async def subscription_extra(
    dialog_manager: DialogManager, subscription: Optional[SubscriptionDto]
) -> dict[str, Any]:
    extra: dict[str, Any] = {"s_inactive": ""}
    if subscription is None:
        return extra
    container: AsyncContainer = dialog_manager.middleware_data[CONTAINER_NAME]
    block = await (await container.get(BlockService)).get(subscription.user_id)
    if block:
        i18n_block: TranslatorRunner = await container.get(TranslatorRunner)
        extra["s_inactive"] = block_banner(i18n_block, block)
        return extra
    settings = (await (await container.get(SettingsDao)).get()).extra.s.inactive
    if not settings.enabled:
        return extra
    if not settings.include_disabled and subscription.status == SubscriptionStatus.DISABLED:
        return extra
    since = inactive_since(subscription, datetime_now())
    if since is None:
        return extra
    i18n: TranslatorRunner = await container.get(TranslatorRunner)
    delete_on = since + timedelta(days=settings.delete_after_days)
    extra["s_inactive"] = i18n.get(
        "msg-s-subscription.inactive", date=delete_on.strftime(DATE_FORMAT)
    )
    return extra
