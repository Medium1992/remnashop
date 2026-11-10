"""Platega: what goes to its API when a payment is created, and how its
callbacks are taken. The buyer goes in ``metadata.user_email``, which the API
now requires (https://docs.platega.io/)."""

import json
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from src.application.dto import PaymentGatewayDto, UserDto
from src.application.dto.payment_gateway import PlategaGatewaySettingsDto
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus
from src.infrastructure.payment_gateways.platega import PlategaGateway

MERCHANT_ID = "11111111-2222-3333-4444-555555555555"
SECRET = "secret"
TRANSACTION_ID = str(uuid4())


def gateway(
    payment_method: Optional[int] = None, payer: Optional[UserDto] = None
) -> tuple[PlategaGateway, list[httpx.Request]]:
    settings = PlategaGatewaySettingsDto(
        merchant_id=MERCHANT_ID, api_key=SecretStr(SECRET), payment_method=payment_method
    )
    data = PaymentGatewayDto(
        type=PaymentGatewayType.PLATEGA, currency=Currency.RUB, settings=settings
    )
    instance = PlategaGateway(data, bot=None, config=None)  # type: ignore[arg-type]
    instance._bot_username = "shop_bot"
    instance.payer = payer
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        # v1 answers with `redirect`, v2 with `url`.
        link = "redirect" if request.url.path == "/transaction/process" else "url"
        return httpx.Response(
            200, json={"transactionId": TRANSACTION_ID, "status": "PENDING", link: "https://pay/x"}
        )

    instance._client = httpx.AsyncClient(
        base_url=instance.API_BASE,
        headers=dict(instance._client.headers),
        transport=httpx.MockTransport(answer),
    )
    return instance, sent


async def create(instance: PlategaGateway) -> None:
    await instance.handle_create_payment(Decimal("199.00"), "Подписка на 30 дней")


def body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)  # type: ignore[no-any-return]


class TestTheBuyer:
    async def test_a_telegram_user_by_telegram_id(self) -> None:
        instance, sent = gateway(payer=UserDto(id=7, telegram_id=123456789, name="A"))
        await create(instance)
        assert body(sent[0])["metadata"] == {
            "userId": "7",
            "user_email": "123456789@telegram.org",
        }

    async def test_the_same_address_every_time(self) -> None:
        payer = UserDto(
            id=7, telegram_id=123456789, name="A", email="a@b.c", is_email_verified=True
        )
        first, sent_first = gateway(payer=payer)
        second, sent_second = gateway(payer=payer)
        await create(first)
        await create(second)
        assert body(sent_first[0])["metadata"] == body(sent_second[0])["metadata"]
        assert body(sent_first[0])["metadata"]["user_email"] == "123456789@telegram.org"

    async def test_a_web_user_by_a_verified_email(self) -> None:
        payer = UserDto(id=8, name="W", email="w@mail.ru", is_email_verified=True)
        instance, sent = gateway(payer=payer)
        await create(instance)
        assert body(sent[0])["metadata"] == {"userId": "8", "user_email": "w@mail.ru"}

    async def test_an_unverified_email_is_not_sent(self) -> None:
        # The payment page asks the buyer instead.
        instance, sent = gateway(payer=UserDto(id=9, name="W", email="w@mail.ru"))
        await create(instance)
        assert body(sent[0])["metadata"] == {"userId": "9"}


class TestTheRequest:
    async def test_without_a_method_the_buyer_picks_one(self) -> None:
        instance, sent = gateway(payer=UserDto(id=7, telegram_id=1, name="A"))
        result = await instance.handle_create_payment(Decimal("199.00"), "Подписка")

        request = sent[0]
        assert request.url == "https://app.platega.io/v2/transaction/process"
        assert request.headers["X-MerchantId"] == MERCHANT_ID
        assert request.headers["X-Secret"] == SECRET
        assert body(request) == {
            "paymentDetails": {"amount": 199.0, "currency": "RUB"},
            "description": "Подписка",
            "return": "https://t.me/shop_bot",
            "failedUrl": "https://t.me/shop_bot",
            "metadata": {"userId": "7", "user_email": "1@telegram.org"},
        }
        assert (str(result.id), result.url) == (TRANSACTION_ID, "https://pay/x")

    async def test_with_a_method(self) -> None:
        instance, sent = gateway(payment_method=2, payer=UserDto(id=7, telegram_id=1, name="A"))
        result = await instance.handle_create_payment(Decimal("199.00"), "Подписка")

        assert sent[0].url == "https://app.platega.io/transaction/process"
        assert body(sent[0])["paymentMethod"] == 2
        assert result.url == "https://pay/x"


class Request:
    def __init__(self, payload: dict[str, Any], headers: dict[str, str]) -> None:
        self._body = json.dumps(payload).encode()
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


class TestCallbacks:
    AUTH = {"X-MerchantId": MERCHANT_ID, "X-Secret": SECRET}

    @pytest.mark.parametrize(
        "status, expected",
        [
            ("CONFIRMED", TransactionStatus.COMPLETED),
            ("CANCELED", TransactionStatus.CANCELED),
            ("CHARGEBACKED", TransactionStatus.REFUNDED),
        ],
    )
    async def test_statuses(self, status: str, expected: TransactionStatus) -> None:
        instance, _ = gateway()
        payload = {"id": TRANSACTION_ID, "status": status, "paymentMethod": 2, "payload": ""}
        result = await instance.handle_webhook(Request(payload, self.AUTH))  # type: ignore[arg-type]
        assert result is not None
        assert (str(result[0]), result[1]) == (TRANSACTION_ID, expected)
        assert instance.selected_payment_method == "2"

    async def test_a_wrong_secret_is_refused(self) -> None:
        instance, _ = gateway()
        payload = {"id": TRANSACTION_ID, "status": "CONFIRMED"}
        headers = {"X-MerchantId": MERCHANT_ID, "X-Secret": "nope"}
        with pytest.raises(PermissionError):
            await instance.handle_webhook(Request(payload, headers))  # type: ignore[arg-type]
