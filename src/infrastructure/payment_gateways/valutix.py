import hashlib
import hmac
import uuid
from decimal import Decimal
from typing import Any, Final, Union
from uuid import UUID

import orjson
from aiogram import Bot
from fastapi import Request
from httpx import AsyncClient, HTTPStatusError
from loguru import logger

from src.application.dto import PaymentGatewayDto, PaymentResultDto
from src.application.dto.payment_gateway import ValutixGatewaySettingsDto
from src.core.config import AppConfig
from src.core.enums import TransactionStatus

from .base import BasePaymentGateway


# https://docs.panel.valutix.kz/ru/docs
class ValutixGateway(BasePaymentGateway):
    _client: AsyncClient

    API_BASE: Final[str] = "https://api.panel.valutix.kz"

    def __init__(self, gateway: PaymentGatewayDto, bot: Bot, config: AppConfig) -> None:
        super().__init__(gateway, bot, config)

        if not isinstance(self.data.settings, ValutixGatewaySettingsDto):
            raise TypeError(
                f"Invalid settings type: expected {ValutixGatewaySettingsDto.__name__}, "
                f"got {type(self.data.settings).__name__}"
            )

        self._client = self._make_client(
            base_url=self.API_BASE,
            headers={"X-Api-Token": self.data.settings.api_key.get_secret_value()},  # type: ignore[union-attr]
        )

    async def handle_create_payment(self, amount: Decimal, details: str) -> PaymentResultDto:
        payload: dict[str, Any] = {
            "amount": str(amount),
            "externalId": str(uuid.uuid4()),
            "purpose": details[:255],
            "successUrl": await self._get_bot_redirect_url(),
            "failUrl": await self._get_bot_redirect_url(),
            "callbackUrl": self.config.get_webhook(self.data.type),
            "currency": self.data.currency.value,
        }
        if self.data.settings.service_id is not None:  # type: ignore[union-attr]
            payload["serviceId"] = self.data.settings.service_id  # type: ignore[union-attr]
        logger.debug(f"Creating Valutix payment payload: {payload}")

        try:
            response = await self._client.post("v1/orders", json=payload)
            response.raise_for_status()
            data = orjson.loads(response.content)
            return self._get_payment_data(data)

        except HTTPStatusError as e:
            logger.error(
                f"HTTP error creating Valutix payment. "
                f"Status: '{e.response.status_code}', Body: {e.response.text}"
            )
            raise
        except (KeyError, orjson.JSONDecodeError) as e:
            logger.error(f"Failed to parse Valutix response. Error: {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error creating Valutix payment: {e}")
            raise

    async def handle_webhook(self, request: Request) -> Union[tuple[UUID, TransactionStatus], None]:
        logger.debug("Received ValutixGateway webhook request")

        raw_body = await request.body()
        await self._verify_webhook(request, raw_body)

        webhook_data = orjson.loads(raw_body)
        logger.debug(f"Valutix webhook data: {webhook_data}")

        payment_id_str = webhook_data.get("id")
        if not payment_id_str:
            raise ValueError("Required field 'id' is missing")

        status = webhook_data.get("status")
        payment_id = UUID(payment_id_str)

        match status:
            case "COMPLETED":
                return payment_id, TransactionStatus.COMPLETED
            case "CHARGEBACK" | "REFUND":
                return payment_id, TransactionStatus.REFUNDED
            # Not final: an order may still become COMPLETED after any of these,
            # and a canceled transaction would not take that payment.
            case "CREATED" | "PENDING" | "CANCELED" | "FAILED" | "EXPIRED" | "BLOCKED":
                logger.debug(f"Valutix webhook status '{status}' — skipping")
                return None
            case _:
                raise ValueError(f"Unsupported Valutix status: {status}")

    def _get_payment_data(self, data: dict[str, Any]) -> PaymentResultDto:
        valutix_id = data.get("id")
        if not valutix_id:
            raise KeyError("Invalid Valutix response: missing 'id'")

        payment_link = data.get("paymentLink")
        if not payment_link:
            raise KeyError("Invalid Valutix response: missing 'paymentLink'")

        return PaymentResultDto(id=UUID(valutix_id), url=str(payment_link))

    async def _verify_webhook(self, request: Request, raw_body: bytes) -> None:
        signature = request.headers.get("X-Signature")
        if not signature:
            raise PermissionError("Valutix webhook missing X-Signature header")

        api_key = self.data.settings.api_key.get_secret_value()  # type: ignore[union-attr]
        expected_signature = hmac.new(
            api_key.encode(),
            raw_body,
            hashlib.sha512,
        ).hexdigest()
        if not hmac.compare_digest(signature.lower(), expected_signature):
            logger.warning("Valutix webhook HMAC signature verification failed")
            raise PermissionError("Valutix webhook verification failed")
