"""Plans, prices and payment in the cabinet; the payment history; promocodes.

Prices always come from the bot's own pricing (the shop's discounts), and the
purchase kind is decided here as the bot decides it: a renewal of the plan the
subscription is on, a change to another one, or a new subscription.
The gateways offered are those of the cabinet's channel (the bot's one when
the cabinet is open as the Mini App), see services/s/channels.py.
"""

from decimal import Decimal
from typing import Any, Optional

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel

from src.application.common import TranslatorHub
from src.application.common.dao import (
    SettingsDao,
    SubscriptionDao,
    TransactionDao,
)
from src.application.dto import (
    PaymentGatewayDto,
    PlanDto,
    PlanSnapshotDto,
    PriceDetailsDto,
    TransactionDto,
    UserDto,
)
from src.application.dto.s_settings import GatewayChannel
from src.application.services import PricingService
from src.application.use_cases.gateways.commands.payment import (
    CreatePayment,
    CreatePaymentDto,
    ProcessPayment,
    ProcessPaymentDto,
)
from src.application.use_cases.plan.queries.match import MatchPlan, MatchPlanDto
from src.application.use_cases.promocode.commands.activate import (
    ActivatePromocode,
    ActivatePromocodeDto,
)
from src.application.use_cases.user.queries.plans import GetAvailablePlans
from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus
from src.core.exceptions import (
    PriceNotFoundError,
    PromocodeAlreadyActivatedError,
    PromocodeExpiredError,
    PromocodeNotAvailableError,
    PromocodeNotFoundError,
)
from src.infrastructure.services.s.channels import GatewayChannels

from .deps import Actor, CurrentActor
from .errors import CabinetError
from .subscriptions import plan_name

router = APIRouter()

_STATUS = {
    TransactionStatus.PENDING: "PENDING",
    TransactionStatus.COMPLETED: "PAID",
    TransactionStatus.CANCELED: "CANCELED",
    TransactionStatus.REFUNDED: "REFUNDED",
    TransactionStatus.FAILED: "FAILED",
}


def channel_of(actor: Actor) -> GatewayChannel:
    return GatewayChannel.BOT if actor.session.mini_app else GatewayChannel.WEB


async def offered_gateways(channels: GatewayChannels, actor: Actor) -> list[PaymentGatewayDto]:
    return [
        g
        for g in await channels.active(channel_of(actor))
        if g.settings is not None and g.settings.is_configured
    ]


def _money(value: Decimal) -> str:
    return format(value.normalize(), "f") if value == value.to_integral() else f"{value:.2f}"


def _discount_source(pricing: PricingService, user: UserDto, percent: int) -> str:
    if percent <= 0:
        return "NONE"
    return "PERSONAL" if pricing.is_largest_discount_personal(user) else "PURCHASE"


# -- the catalog -------------------------------------------------------------


def plan_body(plan: PlanDto, user: UserDto, hub: TranslatorHub) -> dict[str, Any]:
    i18n = hub.get_translator_by_locale(user.language)
    return {
        "id": plan.id,
        "name": i18n.get(plan.name),
        "description": i18n.get(plan.description) if plan.description else None,
        "badge": None,
        "type": plan.type.value,
        "traffic_limit_strategy": plan.traffic_limit_strategy.value,
        "traffic_limit": plan.traffic_limit,
        "device_limit": plan.device_limit,
        "is_trial": plan.is_trial,
        "order_index": plan.order_index,
        "durations": [
            {
                "days": d.days,
                "order_index": d.order_index,
                "prices": [
                    {
                        "currency": p.currency.value,
                        "price": _money(p.price),
                        "discounted_price": None,
                    }
                    for p in d.prices
                ],
            }
            for d in sorted(plan.durations, key=lambda d: d.order_index)
        ],
        "discount_percent": 0,
        "discount_ends_at": None,
        "icon_emoji_id": None,
        "icon_emoji_fallback": None,
    }


async def resolve_plan(
    actor: Actor,
    plan_id: int,
    plan_code: Optional[str],
    get_available_plans: GetAvailablePlans,
) -> PlanDto:
    plans = await get_available_plans.system(actor.user)
    plan = next((p for p in plans if p.id == plan_id and not p.is_trial), None)
    if plan is None:
        raise CabinetError(404, "PLAN_NOT_AVAILABLE", "Plan is not available")
    if plan.public_code and plan_code is not None and plan.public_code != plan_code:
        raise CabinetError(404, "PLAN_NOT_AVAILABLE", "Plan is not available")
    return plan


async def anchor_currency(
    actor: Actor, channels: GatewayChannels, settings_dao: SettingsDao
) -> Currency:
    gateways = await offered_gateways(channels, actor)
    if gateways:
        return gateways[0].currency
    return (await settings_dao.get()).default_currency


@router.get("/plans/catalog")
@inject
async def catalog(
    actor: CurrentActor,
    hub: FromDishka[TranslatorHub],
    settings_dao: FromDishka[SettingsDao],
    channels: FromDishka[GatewayChannels],
    subscription_dao: FromDishka[SubscriptionDao],
    get_available_plans: FromDishka[GetAvailablePlans],
    match_plan: FromDishka[MatchPlan],
    plan_code: Optional[str] = None,
    renew_subscription_id: Optional[int] = None,
) -> dict[str, Any]:
    plans = [p for p in await get_available_plans.system(actor.user) if not p.is_trial]
    if plan_code:
        plans = [p for p in plans if p.public_code == plan_code]
    if renew_subscription_id is not None:
        current = await subscription_dao.get_current(actor.user.id)
        matched = None
        if current is not None and current.id == renew_subscription_id:
            matched = await match_plan.system(
                MatchPlanDto(plan_snapshot=current.plan_snapshot, plans=plans)
            )
        plans = [matched] if matched else []
    plans.sort(key=lambda p: p.order_index)
    return {
        "items": [plan_body(p, actor.user, hub) for p in plans],
        "total": len(plans),
        "anchor_currency": (await anchor_currency(actor, channels, settings_dao)).value,
    }


@router.get("/checkout/methods")
@inject
async def methods(actor: CurrentActor, channels: FromDishka[GatewayChannels]) -> dict[str, Any]:
    return {
        "items": [
            {
                "gateway_type": g.type.value,
                "currency": g.currency.value,
                "requires_email": False,
                "asks_email": False,
            }
            for g in await offered_gateways(channels, actor)
        ]
    }


class QuoteBody(BaseModel):
    plan_id: int
    duration_days: int
    currency: str
    plan_code: Optional[str] = None
    renew_subscription_id: Optional[int] = None
    change_subscription_id: Optional[int] = None


def price_for(
    plan: PlanDto, days: int, currency: Currency, actor: Actor, pricing: PricingService
) -> PriceDetailsDto:
    duration = plan.get_duration(days)
    if duration is None:
        raise CabinetError(404, "PLAN_DURATION_NOT_FOUND", "No such duration")
    try:
        price = duration.get_price(currency)
    except PriceNotFoundError as error:
        raise CabinetError(409, "GATEWAY_CURRENCY_UNSUPPORTED", str(error)) from error
    return pricing.calculate(actor.user, price, currency, apply_discount=not plan.is_trial)


def _currency(raw: str) -> Currency:
    try:
        return Currency(raw.upper())
    except ValueError:
        raise CabinetError(409, "GATEWAY_CURRENCY_UNSUPPORTED", "Unknown currency") from None


def quote_body(
    price: PriceDetailsDto, currency: Currency, actor: Actor, pricing: PricingService
) -> dict[str, Any]:
    return {
        "base_price": _money(price.original_amount),
        "change_credit": "0",
        "change_credit_days": 0,
        "plan_discount_percent": 0,
        "discount_percent": price.discount_percent,
        "discount_source": _discount_source(pricing, actor.user, price.discount_percent),
        "final_price": _money(price.final_amount),
        "currency": currency.value,
    }


@router.post("/checkout/quote")
@inject
async def quote(
    body: QuoteBody,
    actor: CurrentActor,
    pricing: FromDishka[PricingService],
    get_available_plans: FromDishka[GetAvailablePlans],
) -> dict[str, Any]:
    currency = _currency(body.currency)
    plan = await resolve_plan(actor, body.plan_id, body.plan_code, get_available_plans)
    price = price_for(plan, body.duration_days, currency, actor, pricing)
    return quote_body(price, currency, actor, pricing)


class CheckoutBody(BaseModel):
    plan_id: int
    duration_days: int
    currency: str
    purchase_type: Optional[str] = None
    gateway_type: Optional[str] = None
    target_subscription_id: Optional[int] = None
    plan_code: Optional[str] = None
    expected_price: Optional[str] = None


async def purchase_kind(
    actor: Actor,
    plan: PlanDto,
    subscription_dao: SubscriptionDao,
    match_plan: MatchPlan,
) -> PurchaseType:
    """As the bot decides it (routers/subscription/getters.py, plan_getter)."""
    current = await subscription_dao.get_current(actor.user.id)
    if current is None:
        return PurchaseType.NEW
    matched = await match_plan.system(
        MatchPlanDto(plan_snapshot=current.plan_snapshot, plans=[plan])
    )
    if matched and not current.is_unlimited:
        return PurchaseType.RENEW
    return PurchaseType.CHANGE


@router.post("/checkout")
@inject
async def checkout(
    body: CheckoutBody,
    actor: CurrentActor,
    settings_dao: FromDishka[SettingsDao],
    channels: FromDishka[GatewayChannels],
    subscription_dao: FromDishka[SubscriptionDao],
    transaction_dao: FromDishka[TransactionDao],
    pricing: FromDishka[PricingService],
    get_available_plans: FromDishka[GetAvailablePlans],
    match_plan: FromDishka[MatchPlan],
    create_payment: FromDishka[CreatePayment],
    process_payment: FromDishka[ProcessPayment],
) -> dict[str, Any]:
    if not (await settings_dao.get()).access.payments_allowed:
        raise CabinetError(403, "PAYMENTS_DISABLED", "Payments are off")

    offered = await offered_gateways(channels, actor)
    plan = await resolve_plan(actor, body.plan_id, body.plan_code, get_available_plans)
    currency = _currency(body.currency)
    price = price_for(plan, body.duration_days, currency, actor, pricing)
    if body.expected_price is not None and Decimal(body.expected_price) != price.final_amount:
        raise CabinetError(409, "PRICE_CHANGED", "The price has changed")

    gateway: Optional[PaymentGatewayDto]
    if body.gateway_type:
        gateway = next((g for g in offered if g.type.value == body.gateway_type), None)
        if gateway is None:
            raise CabinetError(409, "GATEWAY_NOT_AVAILABLE", "Payment method not available")
    else:
        # only an order that costs nothing comes without one; it is recorded
        # on the first method of its currency, as the bot does
        if not price.is_free:
            raise CabinetError(409, "GATEWAY_REQUIRED", "Choose a payment method")
        gateway = next((g for g in offered if g.currency == currency), None)
        if gateway is None:
            raise CabinetError(409, "GATEWAY_NOT_AVAILABLE", "No payment method")
    if gateway.currency != currency:
        raise CabinetError(409, "GATEWAY_CURRENCY_UNSUPPORTED", "Another currency")

    kind = await purchase_kind(actor, plan, subscription_dao, match_plan)
    snapshot = PlanSnapshotDto.from_plan(plan, body.duration_days)
    payment = await create_payment(
        actor.user,
        CreatePaymentDto(
            plan_snapshot=snapshot,
            pricing=price,
            purchase_type=kind,
            gateway_type=PaymentGatewayType(gateway.type),
        ),
    )
    if price.is_free:
        await process_payment.system(
            ProcessPaymentDto(
                payment_id=payment.id,
                new_transaction_status=TransactionStatus.COMPLETED,
                gateway_type=PaymentGatewayType(gateway.type),
            )
        )
    transaction = await transaction_dao.get_by_payment_id(payment.id)
    if transaction is None or transaction.id is None:
        raise CabinetError(502, "PAYMENT_GATEWAY_ERROR", "The order was not recorded")
    logger.info(
        f"{actor.user.log} Cabinet order '{transaction.id}' "
        f"({kind}, {gateway.type}, {price.final_amount} {currency})"
    )
    return {"order_id": transaction.id, "confirmation_url": payment.url}


@router.post("/checkout/waitlist", status_code=204)
async def waitlist(actor: CurrentActor) -> None:
    return None


# -- history -----------------------------------------------------------------


def transaction_body(
    tx: TransactionDto,
    user: UserDto,
    hub: TranslatorHub,
    current_subscription_id: Optional[int],
) -> dict[str, Any]:
    price = tx.pricing
    return {
        "id": tx.id,
        "status": _STATUS.get(tx.status, "PENDING"),
        "final_price": _money(price.final_amount),
        "base_price": _money(price.original_amount),
        "change_credit": "0",
        "plan_discount_percent": 0,
        "discount_percent": price.discount_percent,
        "discount_source": "NONE" if price.discount_percent <= 0 else "PERSONAL",
        "currency": tx.currency.value,
        "purchase_type": tx.purchase_type.value,
        "gateway_type": tx.gateway_type.value,
        "plan_name": plan_name(hub, user, tx.plan_snapshot.name),
        "plan_icon_emoji_id": None,
        "plan_icon_emoji_fallback": None,
        "duration_days": tx.plan_snapshot.duration,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
        "plan_id": tx.plan_snapshot.id,
        "target_subscription_id": (
            current_subscription_id if tx.purchase_type != PurchaseType.NEW else None
        ),
    }


async def _own_transactions(transaction_dao: TransactionDao, user: UserDto) -> list[TransactionDto]:
    items = [t for t in await transaction_dao.get_by_user(user.id) if not t.is_test]
    items.sort(key=lambda t: (t.created_at is not None, t.created_at, t.id), reverse=True)
    return items


@router.get("/transactions/me")
@inject
async def transactions(
    actor: CurrentActor,
    hub: FromDishka[TranslatorHub],
    transaction_dao: FromDishka[TransactionDao],
    subscription_dao: FromDishka[SubscriptionDao],
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    page, page_size = max(page, 1), min(max(page_size, 1), 100)
    items = await _own_transactions(transaction_dao, actor.user)
    current = await subscription_dao.get_current(actor.user.id)
    current_id = current.id if current else None
    chunk = items[(page - 1) * page_size : page * page_size]
    return {
        "items": [transaction_body(t, actor.user, hub, current_id) for t in chunk],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


@router.get("/transactions/me/{transaction_id}")
@inject
async def transaction(
    transaction_id: int,
    actor: CurrentActor,
    hub: FromDishka[TranslatorHub],
    transaction_dao: FromDishka[TransactionDao],
    subscription_dao: FromDishka[SubscriptionDao],
) -> dict[str, Any]:
    items = await _own_transactions(transaction_dao, actor.user)
    tx = next((t for t in items if t.id == transaction_id), None)
    if tx is None:
        raise CabinetError(404, "PAYMENT_ORDER_NOT_FOUND", "Order not found")
    current = await subscription_dao.get_current(actor.user.id)
    return transaction_body(tx, actor.user, hub, current.id if current else None)


# -- promocodes --------------------------------------------------------------


class PromocodeBody(BaseModel):
    code: str
    target_subscription_id: Optional[int] = None


@router.get("/promocodes/targets")
@inject
async def promocode_targets(
    actor: CurrentActor, subscription_dao: FromDishka[SubscriptionDao], code: str = ""
) -> dict[str, Any]:
    current = await subscription_dao.get_current(actor.user.id)
    ids = [current.id] if current is not None and not current.is_trial else []
    return {"subscription_ids": ids}


@router.post("/promocodes/activations")
@inject
async def activate_promocode(
    body: PromocodeBody,
    actor: CurrentActor,
    activate: FromDishka[ActivatePromocode],
) -> dict[str, Any]:
    try:
        promo = await activate(
            actor.user, ActivatePromocodeDto(code=body.code.strip(), user=actor.user)
        )
    except PromocodeNotFoundError as error:
        raise CabinetError(404, "PROMOCODE_NOT_FOUND", str(error)) from error
    except PromocodeExpiredError as error:
        raise CabinetError(409, "PROMOCODE_EXPIRED", str(error)) from error
    except PromocodeAlreadyActivatedError as error:
        raise CabinetError(409, "PROMOCODE_ALREADY_ACTIVATED", str(error)) from error
    except PromocodeNotAvailableError as error:
        raise CabinetError(409, "PROMOCODE_NOT_AVAILABLE", str(error)) from error
    snapshot = promo.plan_snapshot or {}
    return {
        "code": promo.code,
        "reward_type": promo.reward_type.value,
        "reward": promo.reward,
        "reward_duration_days": snapshot.get("duration"),
        "reward_plan_id": snapshot.get("id"),
        "reward_addon_id": None,
    }
