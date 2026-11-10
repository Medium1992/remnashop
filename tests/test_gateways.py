"""Payment gateways against their current APIs (checked on 2026-11-10): what is
sent when a payment is created and how each webhook is taken."""

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

import httpx
import pytest
from adaptix import Retort, loader
from pydantic import SecretStr

from src.application.dto import PaymentGatewayDto
from src.application.dto.payment_gateway import (
    CryptomusGatewaySettingsDto,
    HeleketGatewaySettingsDto,
    MulenPayGatewaySettingsDto,
    UrlPayGatewaySettingsDto,
    ValutixGatewaySettingsDto,
    WataGatewaySettingsDto,
)
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus
from src.infrastructure.payment_gateways.base import BasePaymentGateway
from src.infrastructure.payment_gateways.cryptomus import CryptomusGateway
from src.infrastructure.payment_gateways.heleket import HeleketGateway
from src.infrastructure.payment_gateways.mulen_pay import MulenPayGateway
from src.infrastructure.payment_gateways.url_pay import UrlPayGateway
from src.infrastructure.payment_gateways.valutix import ValutixGateway
from src.infrastructure.payment_gateways.wata import WataGateway

KEY = "payment-key"
ORDER_UUID = uuid4()
ORDER_ID = str(ORDER_UUID)


class Config:
    def get_webhook(self, gateway_type: PaymentGatewayType) -> str:
        return f"https://shop.example/api/v1/payments/{gateway_type.lower()}"


class Request:
    def __init__(self, body: bytes, headers: Optional[dict[str, str]] = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._body


def make(cls: type, gateway_type: PaymentGatewayType, settings: Any) -> Any:
    data = PaymentGatewayDto(type=gateway_type, currency=Currency.RUB, settings=settings)
    instance = cls(data, bot=None, config=Config())
    instance._bot_username = "shop_bot"
    return instance


def record(instance: BasePaymentGateway, reply: dict[str, Any]) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=reply)

    client = instance._client  # type: ignore[attr-defined]
    instance._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=client.base_url, headers=client.headers, transport=httpx.MockTransport(answer)
    )
    return sent


# ── Cryptomus / Heleket ──────────────────────────────────────────────────────


def md5_sign(text: str) -> str:
    return hashlib.md5((base64.b64encode(text.encode()).decode() + KEY).encode()).hexdigest()


def php_body(fields: str) -> bytes:
    """What the PHP side sends: `fields` as json_encode wrote them, then `sign`."""
    return ("{" + fields + ',"sign":"' + md5_sign("{" + fields + "}") + '"}').encode()


@pytest.fixture(params=[CryptomusGateway, HeleketGateway])
def crypto(request: pytest.FixtureRequest) -> Any:
    if request.param is HeleketGateway:
        settings: Any = HeleketGatewaySettingsDto(merchant_id="m", api_key=SecretStr(KEY))
        return make(HeleketGateway, PaymentGatewayType.HELEKET, settings)
    settings = CryptomusGatewaySettingsDto(merchant_id="m", api_key=SecretStr(KEY))
    return make(CryptomusGateway, PaymentGatewayType.CRYPTOMUS, settings)


def from_crypto(instance: Any, body: bytes) -> Request:
    return Request(body, {"X-Real-IP": instance.NETWORKS[0]})


class TestCryptomusSignature:
    async def test_slashes_escaped_as_php_does(self, crypto: Any) -> None:
        # A URL or a txid with "/" broke the check before.
        fields = (
            f'"type":"payment","uuid":"u-1","order_id":"{ORDER_ID}","amount":"15.00",'
            '"status":"paid","url":"https:\\/\\/pay.example\\/i\\/1"'
        )
        result = await crypto.handle_webhook(from_crypto(crypto, php_body(fields)))
        assert result == (ORDER_UUID, TransactionStatus.COMPLETED)

    async def test_non_ascii_unescaped(self, crypto: Any) -> None:
        fields = f'"order_id":"{ORDER_ID}","status":"paid","comment":"Оплата"'
        result = await crypto.handle_webhook(from_crypto(crypto, php_body(fields)))
        assert result == (ORDER_UUID, TransactionStatus.COMPLETED)

    async def test_non_ascii_escaped_by_default_php(self, crypto: Any) -> None:
        fields = f'"order_id":"{ORDER_ID}","status":"paid","comment":"\\u041e\\u043f"'
        result = await crypto.handle_webhook(from_crypto(crypto, php_body(fields)))
        assert result == (ORDER_UUID, TransactionStatus.COMPLETED)

    async def test_a_body_with_other_bytes_as_before(self, crypto: Any) -> None:
        # Spaces in the body: only a re-encoding of the data can match.
        data = {"order_id": ORDER_ID, "status": "paid"}
        sign = md5_sign(json.dumps(data, separators=(",", ":")))
        body = json.dumps({**data, "sign": sign}, indent=1).encode()
        result = await crypto.handle_webhook(from_crypto(crypto, body))
        assert result == (ORDER_UUID, TransactionStatus.COMPLETED)

    async def test_the_sign_in_front(self, crypto: Any) -> None:
        fields = f'"order_id":"{ORDER_ID}","status":"paid"'
        body = ('{"sign":"' + md5_sign("{" + fields + "}") + '",' + fields + "}").encode()
        result = await crypto.handle_webhook(from_crypto(crypto, body))
        assert result == (ORDER_UUID, TransactionStatus.COMPLETED)

    async def test_a_changed_body_is_refused(self, crypto: Any) -> None:
        fields = f'"order_id":"{ORDER_ID}","status":"cancel"'
        body = php_body(fields).replace(b'"cancel"', b'"paid"')
        with pytest.raises(PermissionError):
            await crypto.handle_webhook(from_crypto(crypto, body))

    async def test_another_key_is_refused(self, crypto: Any) -> None:
        fields = f'"order_id":"{ORDER_ID}","status":"paid"'
        body = php_body(fields)
        crypto.data.settings.api_key = SecretStr("other")
        with pytest.raises(PermissionError):
            await crypto.handle_webhook(from_crypto(crypto, body))

    async def test_another_ip_is_refused(self, crypto: Any) -> None:
        body = php_body(f'"order_id":"{ORDER_ID}","status":"paid"')
        with pytest.raises(PermissionError):
            await crypto.handle_webhook(Request(body, {"X-Real-IP": "1.2.3.4"}))


class TestCryptomusStatuses:
    @pytest.mark.parametrize(
        "status, expected",
        [
            ("paid", TransactionStatus.COMPLETED),
            ("paid_over", TransactionStatus.COMPLETED),
            ("cancel", TransactionStatus.CANCELED),
            ("fail", TransactionStatus.CANCELED),
            ("system_fail", TransactionStatus.CANCELED),
        ],
    )
    async def test_final(self, crypto: Any, status: str, expected: TransactionStatus) -> None:
        body = php_body(f'"order_id":"{ORDER_ID}","status":"{status}"')
        assert await crypto.handle_webhook(from_crypto(crypto, body)) == (ORDER_UUID, expected)

    @pytest.mark.parametrize(
        "status", ["confirm_check", "check", "process", "wrong_amount_waiting"]
    )
    async def test_on_the_way(self, crypto: Any, status: str) -> None:
        body = php_body(f'"order_id":"{ORDER_ID}","status":"{status}"')
        assert await crypto.handle_webhook(from_crypto(crypto, body)) is None

    @pytest.mark.parametrize("status", ["wrong_amount", "locked", "refund_paid"])
    async def test_for_the_admin_to_look_at(self, crypto: Any, status: str) -> None:
        body = php_body(f'"order_id":"{ORDER_ID}","status":"{status}"')
        with pytest.raises(ValueError):
            await crypto.handle_webhook(from_crypto(crypto, body))


# ── WATA ─────────────────────────────────────────────────────────────────────


class TestWata:
    async def run(self, status: str) -> Any:
        settings = WataGatewaySettingsDto(api_key=SecretStr(KEY))
        instance = make(WataGateway, PaymentGatewayType.WATA, settings)

        async def trusted(request: Any, raw_body: bytes) -> bool:
            return True

        instance._verify_webhook = trusted
        body = json.dumps({"orderId": ORDER_ID, "transactionStatus": status}).encode()
        return await instance.handle_webhook(Request(body))

    @pytest.mark.parametrize("status", ["Created", "Pending"])
    async def test_before_the_bank_answers(self, status: str) -> None:
        assert await self.run(status) is None

    async def test_paid_and_declined(self) -> None:
        assert (await self.run("Paid"))[1] == TransactionStatus.COMPLETED
        assert (await self.run("Declined"))[1] == TransactionStatus.CANCELED


# ── Valutix ──────────────────────────────────────────────────────────────────


def valutix(service_id: Optional[int] = None) -> ValutixGateway:
    settings = ValutixGatewaySettingsDto(api_key=SecretStr(KEY), service_id=service_id)
    return make(ValutixGateway, PaymentGatewayType.VALUTIX, settings)  # type: ignore[no-any-return]


class TestValutix:
    async def webhook(self, status: str) -> Any:
        body = json.dumps({"id": ORDER_ID, "status": status}).encode()
        sign = hmac.new(KEY.encode(), body, hashlib.sha512).hexdigest()
        return await valutix().handle_webhook(Request(body, {"X-Signature": sign}))  # type: ignore[arg-type]

    async def test_completed(self) -> None:
        assert (await self.webhook("COMPLETED"))[1] == TransactionStatus.COMPLETED

    @pytest.mark.parametrize("status", ["CHARGEBACK", "REFUND"])
    async def test_money_back(self, status: str) -> None:
        assert (await self.webhook(status))[1] == TransactionStatus.REFUNDED

    @pytest.mark.parametrize(
        "status", ["CREATED", "PENDING", "CANCELED", "FAILED", "EXPIRED", "BLOCKED"]
    )
    async def test_not_final_leaves_it_pending(self, status: str) -> None:
        # Canceled here, a later COMPLETED would not be taken.
        assert await self.webhook(status) is None

    async def test_a_forged_webhook(self) -> None:
        body = json.dumps({"id": ORDER_ID, "status": "COMPLETED"}).encode()
        with pytest.raises(PermissionError):
            await valutix().handle_webhook(Request(body, {"X-Signature": "00"}))  # type: ignore[arg-type]

    async def create(self, instance: ValutixGateway) -> dict[str, Any]:
        sent = record(instance, {"id": ORDER_ID, "paymentLink": "https://pay/x"})
        result = await instance.handle_create_payment(Decimal("199.00"), "Подписка")
        assert result.url == "https://pay/x"
        assert sent[0].url == "https://api.panel.valutix.kz/v1/orders"
        return json.loads(sent[0].content)  # type: ignore[no-any-return]

    async def test_the_service_when_set(self) -> None:
        assert (await self.create(valutix(service_id=42)))["serviceId"] == 42

    async def test_no_service_as_before(self) -> None:
        assert "serviceId" not in await self.create(valutix())

    def test_configured_without_the_service(self) -> None:
        assert ValutixGatewaySettingsDto(api_key=SecretStr(KEY)).is_configured
        assert not ValutixGatewaySettingsDto().is_configured

    def test_settings_saved_before_still_load(self) -> None:
        retort = Retort(recipe=[loader(SecretStr, SecretStr)])
        loaded = retort.load({"api_key": KEY, "display_name": None}, ValutixGatewaySettingsDto)
        assert loaded.service_id is None and loaded.is_configured


# ── MulenPay / UrlPay ────────────────────────────────────────────────────────


def sha1_sign(currency: str, amount: str, shop_id: int) -> str:
    return hashlib.sha1(f"{currency}{amount}{shop_id}{KEY}".encode()).hexdigest()


@pytest.mark.parametrize(
    "cls, gateway_type, settings_cls, url",
    [
        (
            MulenPayGateway,
            PaymentGatewayType.MULENPAY,
            MulenPayGatewaySettingsDto,
            "https://api.mulenpay.com/api/v2/payments",
        ),
        (
            UrlPayGateway,
            PaymentGatewayType.URLPAY,
            UrlPayGatewaySettingsDto,
            "https://urlpay.io/api/v2/payments",
        ),
    ],
)
async def test_mulenpay_family(cls: type, gateway_type: Any, settings_cls: type, url: str) -> None:
    settings = settings_cls(api_key=SecretStr("token"), shop_id=7, secret_key=SecretStr(KEY))
    instance = make(cls, gateway_type, settings)
    sent = record(instance, {"success": True, "paymentUrl": "https://pay/x"})
    await instance.handle_create_payment(Decimal("199"), "Подписка")

    request = sent[0]
    body = json.loads(request.content)
    assert request.url == url
    assert (body["currency"], body["amount"]) == ("rub", "199.00")
    # The sign is over what is sent, and a webhook signed the same way is taken.
    assert body["sign"] == sha1_sign("rub", "199.00", 7)

    hook = {"uuid": body["uuid"], "payment_status": "success", "currency": "rub", "amount": "199"}
    hook["sign"] = sha1_sign("rub", "199.00", 7)
    result = await instance.handle_webhook(Request(json.dumps(hook).encode()))
    assert result[1] == TransactionStatus.COMPLETED
