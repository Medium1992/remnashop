"""Receipts of the self-employed in "Мой налог" (docs/s/README.md)."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Optional
from uuid import UUID

from src.core.enums import PurchaseType


class NalogLogin(StrEnum):
    PASSWORD = "password"
    SMS = "sms"
    # Logged in by hand in a browser (the cabinet asks for a captcha there);
    # the bot only refreshes the session it was given.
    BROWSER = "browser"
    # An access key made in the cabinet for a counterparty: a long-lived
    # token used as it is, with nothing to refresh.
    KEY = "key"


@dataclass(kw_only=True)
class NalogAccountDto:
    """The bot's login to the cabinet, decrypted. Never shown, never logged."""

    inn: Optional[str] = None
    method: Optional[NalogLogin] = None
    phone: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)
    refresh_token: Optional[str] = field(default=None, repr=False)
    token: Optional[str] = field(default=None, repr=False)
    token_expires_at: Optional[datetime] = None
    device_id: str = ""
    proxy: Optional[str] = field(default=None, repr=False)
    # Set when the login stopped working and nobody has logged in since.
    lost_at: Optional[datetime] = None
    lost_reason: Optional[str] = None

    @property
    def logged_in(self) -> bool:
        if self.lost_at is not None:
            return False
        if self.method == NalogLogin.KEY:
            return bool(self.token)
        return bool(self.refresh_token)


class ReceiptStatus(StrEnum):
    # Waits for its turn (again, after a failure that left nothing behind).
    PENDING = "pending"
    # Went out to the cabinet; what came of it is not written yet.
    SENDING = "sending"
    # In the cabinet, not yet with the buyer.
    REGISTERED = "registered"
    DELIVERED = "delivered"
    # Not in the cabinet, and the bot stopped trying.
    FAILED = "failed"
    # Maybe in the cabinet: sent with no clear answer. Someone has to look.
    UNKNOWN = "unknown"
    CANCELED = "canceled"


@dataclass(kw_only=True)
class ReceiptDto:
    id: int
    payment_id: UUID
    user_id: Optional[int]
    amount: Decimal
    paid_at: datetime
    purchase_type: PurchaseType
    duration: int
    status: ReceiptStatus
    name: str = ""
    receipt_uuid: Optional[str] = None
    inn: Optional[str] = None
    # An organisation the receipt is made out to; a private person otherwise.
    client_inn: Optional[str] = None
    client_name: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    delivery_error: Optional[str] = None
    delivered_at: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
