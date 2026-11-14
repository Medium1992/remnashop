"""Bonus days in the S dashboard: the switch and the plan for those without one."""

from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from magic_filter import F

from src.application.common import TranslatorRunner
from src.application.common.dao import PlanDao, SettingsDao
from src.application.dto import TelegramUserDto
from src.application.use_cases.s.settings import SetBonusPlan
from src.core.constants import USER_KEY
from src.core.enums import BannerName
from src.telegram.s_states import SDashboard
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Button, Row, ScrollingGroup, Select, SwitchTo
from src.telegram.window import Window

from .handlers import on_toggle


@inject
async def bonus_getter(
    dialog_manager: DialogManager,
    settings_dao: FromDishka[SettingsDao],
    plan_dao: FromDishka[PlanDao],
    i18n: FromDishka[TranslatorRunner],
    **kwargs: Any,
) -> dict[str, Any]:
    bonus = (await settings_dao.get()).extra.s.bonus
    plans = [plan for plan in await plan_dao.get_all() if not plan.is_trial]
    chosen = next((plan for plan in plans if plan.id == bonus.plan_id), None)
    return {
        "bonus_enabled": int(bonus.enabled),
        "bonus_plan": i18n.get(chosen.name) if chosen else "",
        "bonus_plan_set": int(chosen is not None),
        "bonus_plans": [
            {
                "id": plan.id,
                "name": i18n.get(plan.name),
                "chosen": int(plan.id == bonus.plan_id),
                "active": int(plan.is_active),
            }
            for plan in plans
        ],
    }


@inject
async def on_bonus_plan(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    item_id: str,
    settings_dao: FromDishka[SettingsDao],
    set_bonus_plan: FromDishka[SetBonusPlan],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    plan_id = int(item_id)
    # The chosen plan again clears the choice.
    current = (await settings_dao.get()).extra.s.bonus.plan_id
    await set_bonus_plan(user, None if plan_id == current else plan_id)


bonus = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-dashboard.bonus"),
    Row(
        Button(
            I18nFormat("btn-s.bonus-enabled", enabled=F["bonus_enabled"]),
            id="bonus_enabled",
            on_click=on_toggle,
        )
    ),
    ScrollingGroup(
        Select(
            I18nFormat(
                "btn-s.bonus-plan",
                name=F["item"]["name"],
                chosen=F["item"]["chosen"],
                active=F["item"]["active"],
            ),
            id="bonus_plan",
            item_id_getter=lambda item: item["id"],
            items="bonus_plans",
            on_click=on_bonus_plan,
        ),
        id="bonus_plans",
        width=1,
        height=8,
        hide_on_single_page=True,
    ),
    Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=SDashboard.MAIN)),
    IgnoreUpdate(),
    state=SDashboard.BONUS,
    getter=bonus_getter,
)
