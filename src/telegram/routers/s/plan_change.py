"""The plan change of the "s" edition in the subscription dialog (docs/s/README.md).

Hooked into upstream's purchase at three points only: ``intercept`` when a
duration is chosen, ``change_price`` wherever upstream prices the purchase,
and the two windows below. A purchase other than a change never reaches them.

After the duration: how to count what is left of the current plan (when there
is something to count), then which devices go (when the new plan has fewer).
Both answers ride in the payment's price, so the change applied after payment
is the one that was paid for.
"""

from datetime import timedelta
from typing import Any, Optional

from adaptix import Retort
from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import AsyncContainer
from dishka.integrations.aiogram import CONTAINER_NAME

from src.application.common import Notifier, TranslatorRunner
from src.application.common.dao import SettingsDao
from src.application.dto import MessagePayloadDto, PlanDto, PriceDetailsDto, TelegramUserDto
from src.application.dto.s_plan_change import ChangeMode, PlanChangeDto
from src.application.services import PricingService
from src.application.services.s_plan_change import (
    PlanChange,
    devices_to_drop,
    keeps_expiry,
    quote,
)
from src.core.constants import USER_KEY
from src.core.enums import Currency, PurchaseType
from src.core.exceptions import PriceNotFoundError
from src.core.utils.i18n_helpers import i18n_format_days
from src.core.utils.time import datetime_now
from src.infrastructure.services.s.inactive import local_date
from src.infrastructure.services.s.reminders import device_title
from src.telegram.states import Subscription

_MODE = "s_change_mode"
_DROP = "s_change_drop"
_DEVICES = "s_change_devices"
_MUST = "s_change_must"
_RESUME = "s_change_resume"
DEVICES_WIDGET = "s_change_devices"
BUTTON_DATE = "%d.%m.%Y"


def _container(dialog_manager: DialogManager) -> AsyncContainer:
    return dialog_manager.middleware_data[CONTAINER_NAME]  # type: ignore[no-any-return]


def _user(dialog_manager: DialogManager) -> TelegramUserDto:
    return dialog_manager.middleware_data[USER_KEY]  # type: ignore[no-any-return]


def _is_change(dialog_manager: DialogManager) -> bool:
    return dialog_manager.dialog_data.get("purchase_type") == PurchaseType.CHANGE


async def _plan(dialog_manager: DialogManager) -> PlanDto:
    retort = await _container(dialog_manager).get(Retort)
    return retort.load(dialog_manager.dialog_data[PlanDto.__name__], PlanDto)


async def _base_price(
    dialog_manager: DialogManager, plan: PlanDto, days: int, currency: Currency
) -> Optional[PriceDetailsDto]:
    duration = plan.get_duration(days)
    if duration is None:
        return None
    pricing = await _container(dialog_manager).get(PricingService)
    try:
        return pricing.calculate_for_duration(
            _user(dialog_manager), duration, currency, apply_discount=not plan.is_trial
        )
    except PriceNotFoundError:
        return None


# ── hooks into upstream ──────────────────────────────────────────────────────


async def intercept(dialog_manager: DialogManager, plan: PlanDto, days: int) -> bool:
    """Called when a duration is chosen; True when a window here took over."""
    data = dialog_manager.dialog_data
    if data.pop(_RESUME, False):
        return False

    for key in (_MODE, _DROP, _DEVICES, _MUST):
        data.pop(key, None)
    if not _is_change(dialog_manager):
        return False

    # A price cached for another way of counting must not be reused.
    data.pop("payment_cache", None)

    settings = await (await _container(dialog_manager).get(SettingsDao)).get()
    currency = settings.default_currency
    base = await _base_price(dialog_manager, plan, days, currency)
    if base is not None and not base.is_free:
        change = await _container(dialog_manager).get(PlanChange)
        if await change.leftover(_user(dialog_manager).id, currency):
            await dialog_manager.switch_to(Subscription.S_CHANGE_MODE)
            return True

    return await _ask_devices(dialog_manager, plan)


async def change_price(
    dialog_manager: DialogManager,
    plan: PlanDto,
    days: int,
    currency: Currency,
    price: PriceDetailsDto,
) -> PriceDetailsDto:
    """``price`` of a change as the buyer chose to count it, with its devices."""
    if not _is_change(dialog_manager):
        return price

    data = dialog_manager.dialog_data
    mode = data.get(_MODE)
    # Not offered for this plan; a stale choice counts as a new term instead.
    if mode == ChangeMode.KEEP_EXPIRY and not keeps_expiry(plan):
        mode = ChangeMode.FROM_TODAY
    if mode and not price.is_free:
        change = await _container(dialog_manager).get(PlanChange)
        leftover = await change.leftover(_user(dialog_manager).id, currency)
        # Paid in another currency than the current plan: nothing to count.
        if leftover:
            pricing = await _container(dialog_manager).get(PricingService)
            price = quote(
                price,
                days,
                leftover,
                ChangeMode(mode),
                lambda amount: pricing.apply_currency_rules(amount, currency),
            )

    drop = data.get(_DROP) or []
    if drop:
        price.change = price.change or PlanChangeDto()
        price.change.drop_hwids = list(drop)
    return price


# ── how to count what is left ────────────────────────────────────────────────


async def change_mode_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    container = _container(dialog_manager)
    i18n = await container.get(TranslatorRunner)
    pricing = await container.get(PricingService)
    settings = await (await container.get(SettingsDao)).get()
    currency = settings.default_currency
    plan = await _plan(dialog_manager)
    days: int = dialog_manager.dialog_data["selected_duration"]

    base = await _base_price(dialog_manager, plan, days, currency)
    leftover = await (await container.get(PlanChange)).leftover(_user(dialog_manager).id, currency)
    if base is None or leftover is None:
        # Nothing to count any more (it ran out while the window was open).
        return {"s_keep": 0, "s_today": 0, "s_left_days": 0, "s_left_value": 0, "s_traffic": 0}

    def priced(mode: ChangeMode) -> PriceDetailsDto:
        return quote(
            base,
            days,
            leftover,
            mode,
            lambda amount: pricing.apply_currency_rules(amount, currency),
        )

    keep = priced(ChangeMode.KEEP_EXPIRY) if days > 0 and keeps_expiry(plan) else None
    today = priced(ChangeMode.FROM_TODAY)
    key, kw = i18n_format_days(days)
    new_end = datetime_now() + timedelta(days=days)

    return {
        "s_left_days": int(leftover.days),
        "s_traffic": int(leftover.traffic_left_gb is not None),
        "s_traffic_left": round(leftover.traffic_left_gb or 0, 1),
        "s_traffic_limit": leftover.traffic_limit_gb or 0,
        "s_left_value": pricing.apply_currency_rules(leftover.value, currency),
        "s_currency": currency.symbol,
        "s_keep": int(keep is not None),
        "s_keep_amount": keep.final_amount if keep else 0,
        "s_keep_free": int(keep is not None and keep.is_free),
        "s_keep_date": leftover.expire_at.strftime(BUTTON_DATE),
        "s_keep_date_local": local_date(leftover.expire_at),
        "s_today": 1,
        "s_today_amount": today.final_amount,
        "s_today_free": int(today.is_free),
        "s_today_period": i18n.get(key, **kw),
        "s_today_date_local": local_date(new_end) if days > 0 else "∞",
        "s_full_amount": base.final_amount,
    }


async def on_change_mode(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    mode = ChangeMode.KEEP_EXPIRY if widget.widget_id == "s_change_keep" else ChangeMode.FROM_TODAY
    dialog_manager.dialog_data[_MODE] = mode.value
    if await _ask_devices(dialog_manager, await _plan(dialog_manager)):
        return
    await _resume(callback, widget, dialog_manager)


# ── which devices go ─────────────────────────────────────────────────────────


async def _ask_devices(dialog_manager: DialogManager, plan: PlanDto) -> bool:
    change = await _container(dialog_manager).get(PlanChange)
    devices = await change.devices(_user(dialog_manager).id)
    must = devices_to_drop(devices, plan.device_limit)
    if not must:
        return False

    dialog_manager.dialog_data[_DEVICES] = [
        {"id": str(index), "hwid": device.hwid, "title": device_title(device)}
        for index, device in enumerate(devices)
    ]
    dialog_manager.dialog_data[_MUST] = must
    await dialog_manager.find(DEVICES_WIDGET).reset_checked()  # type: ignore[union-attr]
    await dialog_manager.switch_to(Subscription.S_CHANGE_DEVICES)
    return True


async def change_devices_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    plan = await _plan(dialog_manager)
    devices = dialog_manager.dialog_data.get(_DEVICES, [])
    checked = dialog_manager.find(DEVICES_WIDGET).get_checked()  # type: ignore[union-attr]
    i18n = await _container(dialog_manager).get(TranslatorRunner)
    return {
        "s_devices": devices,
        "s_plan": i18n.get(plan.name),
        "s_limit": plan.device_limit,
        "s_count": len(devices),
        "s_must": dialog_manager.dialog_data.get(_MUST, 0),
        "s_checked": len(checked),
    }


async def on_change_devices_done(
    callback: CallbackQuery, widget: Button, dialog_manager: DialogManager
) -> None:
    must: int = dialog_manager.dialog_data.get(_MUST, 0)
    checked = set(dialog_manager.find(DEVICES_WIDGET).get_checked())  # type: ignore[union-attr]
    if len(checked) < must:
        notifier = await _container(dialog_manager).get(Notifier)
        await notifier.notify_user(
            _user(dialog_manager),
            payload=MessagePayloadDto(
                i18n_key="ntf-s-change.devices-few", i18n_kwargs={"must": must}
            ),
        )
        return

    devices = dialog_manager.dialog_data.get(_DEVICES, [])
    dialog_manager.dialog_data[_DROP] = [d["hwid"] for d in devices if d["id"] in checked]
    await _resume(callback, widget, dialog_manager)


async def _resume(callback: CallbackQuery, widget: Any, dialog_manager: DialogManager) -> None:
    """On to the payment, as upstream goes on after a duration."""
    from src.telegram.routers.subscription.handlers import on_duration_select  # noqa: PLC0415

    dialog_manager.dialog_data[_RESUME] = True
    days: int = dialog_manager.dialog_data["selected_duration"]
    await on_duration_select(callback, widget, dialog_manager, days)  # type: ignore[no-untyped-call]
