import base64
import hashlib
import json
import re
import uuid
from decimal import Decimal
from hmac import compare_digest
from typing import Any, Iterator, Union
from uuid import UUID

import orjson
from aiogram import Bot
from fastapi import Request
from httpx import AsyncClient, HTTPStatusError
from loguru import logger

from src.application.dto import PaymentGatewayDto, PaymentResultDto
from src.application.dto.payment_gateway import (
    CryptomusGatewaySettingsDto,
    HeleketGatewaySettingsDto,
)
from src.core.config import AppConfig
from src.core.enums import Currency, TransactionStatus

from .base import BasePaymentGateway


# https://doc.cryptomus.com/
class CryptomusGateway(BasePaymentGateway):
    _client: AsyncClient

    API_BASE = "https://api.cryptomus.com"
    CURRENCY = Currency.USD

    NETWORKS = ["91.227.144.54"]

    # `"sign":"…"` with its comma, wherever in the body it is.
    _SIGN_FIELD = re.compile(r',\s*"sign"\s*:\s*"[0-9a-fA-F]*"|"sign"\s*:\s*"[0-9a-fA-F]*"\s*,?')

    def __init__(self, gateway: PaymentGatewayDto, bot: Bot, config: AppConfig) -> None:
        super().__init__(gateway, bot, config)

        if not isinstance(
            self.data.settings, (CryptomusGatewaySettingsDto, HeleketGatewaySettingsDto)
        ):
            raise TypeError(
                f"Invalid settings type: expected {CryptomusGatewaySettingsDto.__name__} "
                f"or {HeleketGatewaySettingsDto.__name__}, got {type(self.data.settings).__name__}"
            )

        self._client = self._make_client(
            base_url=self.API_BASE,
            headers={"merchant": self.data.settings.merchant_id},  # type: ignore[dict-item]
        )

    async def handle_create_payment(self, amount: Decimal, details: str) -> PaymentResultDto:
        payload = await self._create_payment_payload(str(amount), str(uuid.uuid4()))
        body = json.dumps(payload)
        headers = {"sign": self._generate_signature(body), "Content-Type": "application/json"}
        logger.debug(f"Creating payment payload: {payload}")

        try:
            response = await self._client.post(
                "v1/payment", content=body.encode("utf-8"), headers=headers
            )
            response.raise_for_status()
            data = orjson.loads(response.content).get("result", {})
            return self._get_payment_data(data)

        except HTTPStatusError as e:
            logger.error(
                f"HTTP error creating payment. "
                f"Status: '{e.response.status_code}', Body: {e.response.text}"
            )
            raise
        except (KeyError, orjson.JSONDecodeError) as e:
            logger.error(f"Failed to parse response. Error: {e}")
            raise
        except Exception as e:
            logger.exception(f"An unexpected error occurred while creating payment: {e}")
            raise

    async def handle_webhook(self, request: Request) -> Union[tuple[UUID, TransactionStatus], None]:
        logger.debug(f"Received {self.__class__.__name__} webhook request")
        webhook_data = await self._get_webhook_data(request)

        if not self._verify_webhook(request, webhook_data, await request.body()):
            raise PermissionError("Webhook verification failed")

        status = webhook_data.get("status")
        payment_id_str = webhook_data.get("order_id")

        if not payment_id_str:
            raise ValueError("Required field 'order_id' is missing")

        payment_id = UUID(payment_id_str)

        match status:
            case "paid" | "paid_over":
                transaction_status = TransactionStatus.COMPLETED
            case "cancel" | "fail" | "system_fail":
                transaction_status = TransactionStatus.CANCELED
            case "confirm_check" | "check" | "process" | "wrong_amount_waiting":
                logger.debug(f"Received {status} webhook for {self.__class__.__name__}, skipping")
                return None
            # wrong_amount, locked and refund_* stay errors: the admin has to look.
            case _:
                raise ValueError(f"Unsupported status: {status}")

        return payment_id, transaction_status

    async def _create_payment_payload(self, amount: str, order_id: str) -> dict[str, Any]:
        return {
            "amount": amount,
            "currency": self.CURRENCY,
            "order_id": order_id,
            "url_return": await self._get_bot_redirect_url(),
            "url_success": await self._get_bot_redirect_url(),
            "url_callback": self.config.get_webhook(self.data.type),
            "lifetime": 1800,
            "is_payment_multiple": False,
        }

    def _generate_signature(self, data: str) -> str:
        base64_encoded = base64.b64encode(data.encode("utf-8")).decode()
        raw_string = f"{base64_encoded}{self.data.settings.api_key.get_secret_value()}"  # type: ignore[union-attr]
        return hashlib.md5(raw_string.encode()).hexdigest()

    def _get_payment_data(self, data: dict[str, Any]) -> PaymentResultDto:
        payment_id_str = data.get("order_id")

        if not payment_id_str:
            raise KeyError("Invalid response from API: missing 'order_id'")

        payment_url = data.get("url")

        if not payment_url:
            raise KeyError("Invalid response from API: missing 'url'")

        return PaymentResultDto(id=UUID(payment_id_str), url=str(payment_url))

    def _verify_webhook(self, request: Request, data: dict, raw_body: bytes) -> bool:
        ip = self._get_ip(request.headers)

        if not self._is_ip_trusted(ip):
            logger.critical(f"Webhook received from untrusted IP: '{ip}'")
            return False

        sign = data.pop("sign", None)
        if not sign:
            raise ValueError("Missing signature")

        for signed in self._signed_bodies(data, raw_body):
            if compare_digest(self._generate_signature(signed), str(sign)):
                return True

        logger.warning("Invalid signature")
        return False

    def _signed_bodies(self, data: dict, raw_body: bytes) -> Iterator[str]:
        # The sign is md5 over the body as PHP's json_encode wrote it, without
        # `sign`: slashes as `\/`, non-ASCII as is. The body as it came, minus
        # the field, is that text exactly; the re-encodings cover a body whose
        # bytes differ, and the last one is how it was checked before.
        raw = raw_body.decode("utf-8", errors="replace")
        unsigned, found = self._SIGN_FIELD.subn("", raw, count=1)
        if found:
            yield unsigned
        for ensure_ascii in (False, True):
            compact = json.dumps(data, separators=(",", ":"), ensure_ascii=ensure_ascii)
            yield compact.replace("/", "\\/")
        yield json.dumps(data, separators=(",", ":"))
