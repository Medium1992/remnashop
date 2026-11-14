"""Bonus days in the invite window (docs/s/README.md)."""

from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier, TranslatorRunner
from src.application.common.dao import PlanDao, SettingsDao
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.core.constants import USER_KEY
from src.infrastructure.services.s.bonus import BonusDays, BonusDaysError
from src.infrastructure.services.s.inactive import local_date


async def bonus_extra(
    bonus: BonusDays,
    settings_dao: SettingsDao,
    plan_dao: PlanDao,
    user: TelegramUserDto,
    i18n: TranslatorRunner,
) -> dict[str, Any]:
    """What the invite window shows of the days kept; nothing while it is off."""
    settings = await settings_dao.get()
    if not settings.extra.s.bonus.enabled or not settings.referral.reward.is_extra_days:
        return {"s_bonus": 0, "s_bonus_days": 0}
    plan_id = settings.extra.s.bonus.plan_id
    plan = await plan_dao.get_by_id(plan_id) if plan_id is not None else None
    return {
        "s_bonus": 1,
        "s_bonus_days": await bonus.kept(user.id),
        "s_bonus_plan": i18n.get(plan.name) if plan else "",
        "s_bonus_plan_set": int(plan is not None),
    }


@inject
async def on_take_bonus(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    bonus: FromDishka[BonusDays],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    try:
        taken = await bonus.take(user)
    except BonusDaysError as error:
        await notifier.notify_user(user, i18n_key=f"ntf-s-bonus.{error}")
        return
    except Exception as error:
        logger.exception(f"{user.log} Bonus days not taken: {error}")
        await notifier.notify_user(user, i18n_key="ntf-s-bonus.failed")
        return
    await notifier.notify_user(
        user,
        payload=MessagePayloadDto(
            i18n_key="ntf-s-bonus.taken",
            i18n_kwargs={"days": taken.days, "until": local_date(taken.expire_at)},
        ),
    )
