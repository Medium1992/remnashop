"""The customer's subscription, its devices and the trial.

The bot keeps one subscription a person: the list has it or is empty, and a
subscription id in a path must be that one.
"""

from typing import Any, Optional

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel

from src.application.common import Remnawave, TranslatorHub
from src.application.common.dao import SettingsDao, SubscriptionDao
from src.application.dto import PlanSnapshotDto, SubscriptionDto, UserDto
from src.application.use_cases.remnawave.commands.management import (
    DeleteUserAllDevices,
    DeleteUserDevice,
    DeleteUserDeviceDto,
    ReissueSubscription,
)
from src.application.use_cases.subscription.commands.purchase import (
    ActivateTrialSubscription,
    ActivateTrialSubscriptionDto,
)
from src.application.use_cases.user.queries.plans import GetAvailableTrial
from src.core.enums import SubscriptionStatus
from src.core.exceptions import CooldownError, TrialNotAvailableError

from .deps import CurrentActor
from .errors import CabinetError

router = APIRouter()


def plan_name(hub: TranslatorHub, user: UserDto, name: str) -> str:
    return hub.get_translator_by_locale(user.language).get(name)


async def subscription_body(
    subscription: SubscriptionDto,
    user: UserDto,
    hub: TranslatorHub,
    remnawave: Remnawave,
) -> dict[str, Any]:
    used: Optional[float] = None
    try:
        remna_user = await remnawave.get_user_by_uuid(subscription.user_remna_id)
        used = remna_user.used_traffic_bytes if remna_user else None
    except Exception:
        logger.warning(f"Traffic of {user.log} not read from the panel")
    snapshot = subscription.plan_snapshot
    return {
        "id": subscription.id,
        "status": subscription.current_status.value,
        "is_trial": subscription.is_trial,
        "expire_at": subscription.expire_at.isoformat(),
        "url": subscription.url,
        "effective_traffic_limit": subscription.traffic_limit,
        "effective_device_limit": subscription.device_limit,
        "traffic_limit_strategy": subscription.traffic_limit_strategy.value,
        "plan_name": plan_name(hub, user, snapshot.name),
        "plan_icon_emoji_id": None,
        "plan_icon_emoji_fallback": None,
        "plan_id": snapshot.id,
        "plan_traffic_limit": snapshot.traffic_limit,
        "plan_device_limit": snapshot.device_limit,
        "plan_traffic_limit_strategy": snapshot.traffic_limit_strategy.value,
        "used_traffic": int(used) if used is not None else None,
        "device_all_reset_at": _iso(subscription.device_all_reset_at),
        "link_reset_at": _iso(subscription.link_reset_at),
        "paused_by_channel_leave": subscription.disabled_by_channel_leave,
        "connect_signature": "",
    }


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


async def own_subscription(
    subscription_dao: SubscriptionDao, user: UserDto, subscription_id: int
) -> SubscriptionDto:
    subscription = await subscription_dao.get_current(user.id)
    if subscription is None or subscription.id != subscription_id:
        raise CabinetError(404, "SUBSCRIPTION_NOT_FOUND", "Subscription not found")
    if subscription.current_status == SubscriptionStatus.DELETED:
        raise CabinetError(409, "SUBSCRIPTION_DELETED", "Subscription deleted")
    return subscription


@router.get("/subscriptions/me")
@inject
async def my_subscriptions(
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    hub: FromDishka[TranslatorHub],
    remnawave: FromDishka[Remnawave],
) -> dict[str, Any]:
    subscription = await subscription_dao.get_current(actor.user.id)
    if subscription is None or subscription.current_status == SubscriptionStatus.DELETED:
        return {"items": [], "total": 0}
    body = await subscription_body(subscription, actor.user, hub, remnawave)
    return {"items": [body], "total": 1}


@router.get("/subscriptions/me/trial")
@inject
async def trial(
    actor: CurrentActor,
    settings_dao: FromDishka[SettingsDao],
    hub: FromDishka[TranslatorHub],
    get_available_trial: FromDishka[GetAvailableTrial],
) -> dict[str, Any]:
    plan = await get_available_trial.system(actor.user)
    if plan is None or not plan.durations:
        return {
            "is_available": False,
            "plan_id": None,
            "plan_name": None,
            "duration_days": None,
            "is_free": True,
            "price": None,
            "currency": None,
            "prices": [],
        }
    duration = plan.durations[0]
    currency = (await settings_dao.get()).default_currency
    prices = [{"currency": p.currency.value, "price": str(p.price)} for p in duration.prices]
    price = next((p.price for p in duration.prices if p.currency == currency), None)
    is_free = all(p.price == 0 for p in duration.prices)
    return {
        "is_available": True,
        "plan_id": plan.id,
        "plan_name": plan_name(hub, actor.user, plan.name),
        "duration_days": duration.days,
        # a paid trial is bought at checkout like a plan
        "is_free": is_free,
        "price": None if is_free or price is None else str(price),
        "currency": None if is_free else currency.value,
        "prices": [] if is_free else prices,
    }


class TrialBody(BaseModel):
    mini_app: bool = False


@router.post("/subscriptions/me/actions/activate-trial")
@inject
async def activate_trial(
    body: TrialBody,
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    hub: FromDishka[TranslatorHub],
    remnawave: FromDishka[Remnawave],
    get_available_trial: FromDishka[GetAvailableTrial],
    activate: FromDishka[ActivateTrialSubscription],
) -> dict[str, Any]:
    plan = await get_available_trial.system(actor.user)
    if plan is None or not plan.durations:
        raise CabinetError(409, "TRIAL_NOT_AVAILABLE", "Trial is not available")
    duration = plan.durations[0]
    if any(p.price != 0 for p in duration.prices):
        raise CabinetError(409, "TRIAL_NOT_AVAILABLE", "This trial is bought at checkout")
    try:
        await activate.system(
            ActivateTrialSubscriptionDto(
                user=actor.user, plan=PlanSnapshotDto.from_plan(plan, duration.days)
            )
        )
    except TrialNotAvailableError as error:
        raise CabinetError(409, "TRIAL_NOT_AVAILABLE", str(error)) from error
    subscription = await subscription_dao.get_current(actor.user.id)
    if subscription is None:
        raise CabinetError(409, "TRIAL_NOT_AVAILABLE", "Trial is being set up")
    return await subscription_body(subscription, actor.user, hub, remnawave)


@router.get("/subscriptions/me/{subscription_id}/devices")
@inject
async def devices(
    subscription_id: int,
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    remnawave: FromDishka[Remnawave],
) -> dict[str, Any]:
    subscription = await own_subscription(subscription_dao, actor.user, subscription_id)
    items = await remnawave.get_devices(subscription.user_remna_id)
    return {
        "items": [
            {
                "hwid": device.hwid,
                "platform": device.platform,
                "device_model": device.device_model,
                "user_agent": device.user_agent,
                "created_at": device.created_at.isoformat(),
            }
            for device in items
        ],
        "total": len(items),
    }


@router.delete("/subscriptions/me/{subscription_id}/devices/{hwid}", status_code=204)
@inject
async def delete_device(
    subscription_id: int,
    hwid: str,
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    delete_device: FromDishka[DeleteUserDevice],
) -> None:
    await own_subscription(subscription_dao, actor.user, subscription_id)
    try:
        await delete_device(actor.user, DeleteUserDeviceDto(user_id=actor.user.id, hwid=hwid))
    except CooldownError as error:
        raise _cooldown(error) from error
    except ValueError as error:
        raise CabinetError(409, "DEVICE_RESET_DISABLED", str(error)) from error


@router.post("/subscriptions/me/{subscription_id}/devices/bulk/delete", status_code=204)
@inject
async def delete_devices(
    subscription_id: int,
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    delete_all: FromDishka[DeleteUserAllDevices],
) -> None:
    await own_subscription(subscription_dao, actor.user, subscription_id)
    try:
        await delete_all(actor.user)
    except CooldownError as error:
        raise _cooldown(error) from error
    except ValueError as error:
        raise CabinetError(409, "DEVICE_RESET_DISABLED", str(error)) from error


@router.post("/subscriptions/me/{subscription_id}/actions/reissue", status_code=204)
@inject
async def reissue(
    subscription_id: int,
    actor: CurrentActor,
    subscription_dao: FromDishka[SubscriptionDao],
    reissue_subscription: FromDishka[ReissueSubscription],
) -> None:
    await own_subscription(subscription_dao, actor.user, subscription_id)
    try:
        await reissue_subscription(actor.user)
    except CooldownError as error:
        raise _cooldown(error) from error
    except ValueError as error:
        raise CabinetError(409, "SUBSCRIPTION_DISABLED", str(error)) from error


def _cooldown(error: CooldownError) -> CabinetError:
    available = getattr(error, "available_at", None)
    extra = {"available_at": available.isoformat()} if available is not None else {}
    return CabinetError(429, "COOLDOWN_ACTIVE", str(error), **extra)
