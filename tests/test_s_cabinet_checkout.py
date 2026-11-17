"""Paying in the web cabinet: the channel's gateways, the bot's prices."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

import httpx
import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI

from src.application.common import TranslatorHub
from src.application.common.dao import (
    PaymentGatewayDao,
    SettingsDao,
    SubscriptionDao,
    TransactionDao,
    UserDao,
)
from src.application.dto import PlanDto, PlanDurationDto, PlanPriceDto, UserDto
from src.application.dto.s_settings import GatewayChannelsSettingsDto
from src.application.services import PricingService
from src.application.use_cases.gateways.commands.payment import CreatePayment, ProcessPayment
from src.application.use_cases.plan.queries.match import MatchPlan
from src.application.use_cases.user.queries.plans import GetAvailablePlans
from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus
from src.core.utils import s_cabinet
from src.core.utils.s_cabinet import CabinetConfig
from src.infrastructure.services.s.cabinet_sessions import CabinetSessions
from src.infrastructure.services.s.channels import GatewayChannels
from src.web.s_cabinet import mount_cabinet
from tests.test_s_cabinet_auth import SECRET, FakeRedis

RUB, XTR = Currency.RUB, Currency.XTR


def gateway(kind: str, currency: Currency) -> Any:
    return SimpleNamespace(
        type=PaymentGatewayType(kind),
        currency=currency,
        is_active=True,
        settings=SimpleNamespace(is_configured=True, display_name=None),
    )


def plan(plan_id: int = 1, rub: int = 300, stars: int = 150) -> PlanDto:
    return PlanDto(
        id=plan_id,
        name=f"plan-{plan_id}",
        is_active=True,
        durations=[
            PlanDurationDto(
                days=30,
                prices=[
                    PlanPriceDto(currency=RUB, price=Decimal(rub)),
                    PlanPriceDto(currency=XTR, price=Decimal(stars)),
                ],
            )
        ],
    )


class World:
    def __init__(self) -> None:
        self.user = UserDto(id=7, telegram_id=700, name="A")
        self.gateways = [gateway("TELEGRAM_STARS", XTR), gateway("YOOKASSA", RUB)]
        self.plans = [plan()]
        self.subscription: Optional[Any] = None
        self.created: list[Any] = []
        self.processed: list[Any] = []
        self.transactions: dict[Any, Any] = {}
        self.payments_allowed = True

    # daos and use cases ------------------------------------------------------

    async def get_by_id(self, user_id: int) -> Any:
        return self.user if user_id == self.user.id else None

    async def get(self) -> Any:
        return SimpleNamespace(
            access=SimpleNamespace(payments_allowed=self.payments_allowed),
            default_currency=XTR,
            extra=SimpleNamespace(s=SimpleNamespace(channels=GatewayChannelsSettingsDto())),
        )

    async def get_active(self) -> list[Any]:
        return self.gateways

    async def get_current(self, user_id: int) -> Any:
        return self.subscription

    async def get_by_payment_id(self, payment_id: Any) -> Any:
        return self.transactions.get(payment_id)


class Available:
    def __init__(self, world: World) -> None:
        self.world = world

    @property
    def system(self) -> Any:
        async def run(user: Any) -> list[PlanDto]:
            return self.world.plans

        return run


class Create:
    def __init__(self, world: World) -> None:
        self.world = world

    async def __call__(self, actor: Any, data: Any) -> Any:
        payment_id = uuid4()
        self.world.created.append(data)
        self.world.transactions[payment_id] = SimpleNamespace(id=len(self.world.created))
        url = None if data.pricing.is_free else f"https://pay.example.com/{payment_id}"
        return SimpleNamespace(id=payment_id, url=url)


class Process:
    def __init__(self, world: World) -> None:
        self.world = world

    @property
    def system(self) -> Any:
        async def run(data: Any) -> None:
            self.world.processed.append(data)

        return run


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def sessions() -> CabinetSessions:
    return CabinetSessions(FakeRedis(), b"k" * 32)  # type: ignore[arg-type]


@pytest.fixture
async def client(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch, world: World, sessions: CabinetSessions
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("CABINET_DOMAIN", "cabinet.example.com")
    monkeypatch.setenv("CABINET_PROXY_SECRET", SECRET)
    s_cabinet.cabinet_config.cache_clear()

    class Fakes(Provider):
        scope = Scope.APP

        @provide
        def config(self) -> CabinetConfig:
            return CabinetConfig(domain="cabinet.example.com", proxy_secret=SECRET)

        @provide
        def store(self) -> CabinetSessions:
            return sessions

        @provide
        def users(self) -> UserDao:
            return world  # type: ignore[return-value]

        @provide
        def settings(self) -> SettingsDao:
            return world  # type: ignore[return-value]

        @provide
        def subscriptions(self) -> SubscriptionDao:
            return world  # type: ignore[return-value]

        @provide
        def transactions(self) -> TransactionDao:
            return world  # type: ignore[return-value]

        @provide
        def gateways(self) -> PaymentGatewayDao:
            return world  # type: ignore[return-value]

        @provide
        def channels(self, gateways: PaymentGatewayDao, settings: SettingsDao) -> GatewayChannels:
            return GatewayChannels(None, gateways, settings, None)  # type: ignore[arg-type]

        @provide
        def pricing(self) -> PricingService:
            return PricingService()

        @provide
        def hub(self) -> TranslatorHub:
            runner = SimpleNamespace(get=lambda key, **kw: key)
            hub: Any = SimpleNamespace(get_translator_by_locale=lambda locale: runner)
            return hub  # type: ignore[no-any-return]

        @provide
        def available(self) -> GetAvailablePlans:
            return Available(world)  # type: ignore[return-value]

        @provide
        def match(self) -> MatchPlan:
            return MatchPlan()

        @provide
        def create(self) -> CreatePayment:
            return Create(world)  # type: ignore[return-value]

        @provide
        def process(self) -> ProcessPayment:
            return Process(world)  # type: ignore[return-value]

    app = FastAPI()
    mount_cabinet(app)
    container = make_async_container(Fakes())
    setup_dishka(container, app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"X-Cabinet-Proxy-Secret": SECRET},
    ) as http:
        yield http
    await container.close()
    s_cabinet.cabinet_config.cache_clear()


async def signed_in(sessions: CabinetSessions, mini_app: bool = False) -> dict[str, str]:
    _, issued = await sessions.open(7, mini_app=mini_app)
    return {"Authorization": f"Bearer {issued.access_token}"}


def order(**kw: Any) -> dict[str, Any]:
    return {"plan_id": 1, "duration_days": 30, "currency": "RUB", "gateway_type": "YOOKASSA", **kw}


async def test_a_browser_is_offered_the_cabinets_gateways(
    client: httpx.AsyncClient, sessions: CabinetSessions
) -> None:
    body = (await client.get("/api/v1/checkout/methods", headers=await signed_in(sessions))).json()
    assert [m["gateway_type"] for m in body["items"]] == ["YOOKASSA"]


async def test_the_mini_app_is_offered_the_bots_gateways(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    headers = await signed_in(sessions, mini_app=True)
    body = (await client.get("/api/v1/checkout/methods", headers=headers)).json()
    assert [m["gateway_type"] for m in body["items"]] == ["TELEGRAM_STARS"]
    refused = await client.post("/api/v1/checkout", json=order(), headers=headers)
    assert refused.status_code == 409 and refused.json()["code"] == "GATEWAY_NOT_AVAILABLE"
    assert not world.created


async def test_stars_cannot_be_ordered_from_a_browser(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    stars = order(currency="XTR", gateway_type="TELEGRAM_STARS")
    response = await client.post("/api/v1/checkout", json=stars, headers=await signed_in(sessions))
    assert response.json()["code"] == "GATEWAY_NOT_AVAILABLE" and not world.created


async def test_a_paid_order_goes_to_the_gateway(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    response = await client.post(
        "/api/v1/checkout", json=order(expected_price="300"), headers=await signed_in(sessions)
    )
    body = response.json()
    assert response.status_code == 200
    assert body["order_id"] == 1 and body["confirmation_url"].startswith("https://pay.example.com/")
    (created,) = world.created
    assert created.purchase_type == PurchaseType.NEW
    assert created.pricing.final_amount == Decimal(300)
    assert created.gateway_type == PaymentGatewayType.YOOKASSA
    assert not world.processed


async def test_the_price_is_the_bots_with_the_discount(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    world.user.personal_discount = 10
    quote = await client.post(
        "/api/v1/checkout/quote",
        json={"plan_id": 1, "duration_days": 30, "currency": "RUB"},
        headers=await signed_in(sessions),
    )
    body = quote.json()
    assert body["base_price"] == "300" and body["final_price"] == "270"
    assert body["discount_percent"] == 10 and body["discount_source"] == "PERSONAL"


async def test_a_moved_price_is_refused(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    response = await client.post(
        "/api/v1/checkout", json=order(expected_price="250"), headers=await signed_in(sessions)
    )
    assert response.json()["code"] == "PRICE_CHANGED" and not world.created


async def test_a_free_order_is_settled_at_once(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    world.user.personal_discount = 100
    response = await client.post(
        "/api/v1/checkout",
        json=order(gateway_type=None, expected_price="0"),
        headers=await signed_in(sessions),
    )
    assert response.status_code == 200 and response.json()["confirmation_url"] is None
    (processed,) = world.processed
    assert processed.new_transaction_status == TransactionStatus.COMPLETED


async def test_a_paid_order_needs_a_gateway(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    response = await client.post(
        "/api/v1/checkout", json=order(gateway_type=None), headers=await signed_in(sessions)
    )
    assert response.json()["code"] == "GATEWAY_REQUIRED" and not world.created


async def test_the_same_plan_renews_another_changes(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    from src.application.dto import PlanSnapshotDto  # noqa: PLC0415

    world.plans = [plan(1), plan(2, rub=500)]
    world.subscription = SimpleNamespace(
        id=3,
        is_unlimited=False,
        is_trial=False,
        expire_at=datetime.now(timezone.utc) + timedelta(days=5),
        plan_snapshot=PlanSnapshotDto.from_plan(world.plans[0], 30),
    )
    headers = await signed_in(sessions)
    await client.post("/api/v1/checkout", json=order(plan_id=1), headers=headers)
    await client.post("/api/v1/checkout", json=order(plan_id=2), headers=headers)
    assert [c.purchase_type for c in world.created] == [PurchaseType.RENEW, PurchaseType.CHANGE]


async def test_a_plan_not_offered_to_the_user_is_refused(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    response = await client.post(
        "/api/v1/checkout", json=order(plan_id=9), headers=await signed_in(sessions)
    )
    assert response.json()["code"] == "PLAN_NOT_AVAILABLE" and not world.created


async def test_payments_off(
    client: httpx.AsyncClient, sessions: CabinetSessions, world: World
) -> None:
    world.payments_allowed = False
    response = await client.post(
        "/api/v1/checkout", json=order(), headers=await signed_in(sessions)
    )
    assert response.json()["code"] == "PAYMENTS_DISABLED" and not world.created


async def test_no_session_no_checkout(client: httpx.AsyncClient, world: World) -> None:
    response = await client.post("/api/v1/checkout", json=order())
    assert response.status_code == 401 and not world.created
