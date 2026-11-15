"""Client of the "Мой налог" cabinet of the self-employed (lknpd.nalog.ru).

The tax service publishes no API for a self-employed person; this is the API
of its own web cabinet, the one https://lknpd.nalog.ru talks to. Written here
from scratch after the requests of the open-source clients (shoman4eg/moy-nalog,
alexstep/moy-nalog, kostromin59/lknpd), with no library behind it.

Only HTTP is here: no database, no settings. What a failure means for a
receipt matters most, so the errors say it:

- ``NalogAuthError``: the login is gone (or was wrong); nothing will work
  until someone logs in again.
- ``NalogUnreachableError``: the request never reached the cabinet, or the cabinet
  turned it away before doing anything; trying again is safe.
- ``NalogUnknownError``: the request went out and no clear answer came back. An
  income may have been registered, so it must not be sent again blindly --
  that would be a second receipt for one payment.
- ``NalogError``: the cabinet refused the request and said why.
"""

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Optional

import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyConnector, ProxyError, ProxyTimeoutError
from loguru import logger

from src.core.utils.time import datetime_now

BASE_URL: Final[str] = "https://lknpd.nalog.ru/api"
SITE_URL: Final[str] = "https://lknpd.nalog.ru"
TIMEOUT_SECONDS: Final[float] = 30.0
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
APP_VERSION: Final[str] = "1.0.0"
# Times go to the cabinet with Moscow's offset, as the cabinet itself shows them.
MOSCOW: Final[timezone] = timezone(timedelta(hours=3), "MSK")
# A token the cabinet did not date is taken to live this long.
DEFAULT_TOKEN_TTL: Final[timedelta] = timedelta(minutes=50)

# The cabinet's own wording; it takes no other.
CANCEL_WRONG: Final[str] = "Чек сформирован ошибочно"
CANCEL_REFUND: Final[str] = "Возврат средств"

# Failures to connect at all, the proxy's included: nothing was sent.
_NOT_CONNECTED: Final = (
    aiohttp.ClientConnectorError,
    ProxyConnectionError,
    ProxyTimeoutError,
    ProxyError,
)

# Gateway answers that come before the cabinet does anything.
_NOT_REACHED_STATUSES: Final[frozenset[int]] = frozenset({429, 502, 503})


class NalogError(Exception):
    def __init__(self, message: str, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

    def __str__(self) -> str:
        parts = [str(self.status) if self.status else "", self.code, self.message]
        return " ".join(part for part in parts if part)


class NalogAuthError(NalogError):
    pass


class NalogUnreachableError(NalogError):
    pass


class NalogUnknownError(NalogError):
    pass


@dataclass(frozen=True)
class NalogSession:
    token: str = field(repr=False)
    expires_at: datetime
    refresh_token: str = field(repr=False)
    inn: Optional[str] = None


@dataclass(frozen=True)
class SmsChallenge:
    token: str = field(repr=False)
    expires_in: int


@dataclass(frozen=True)
class ReceiptFile:
    content: bytes = field(repr=False)
    content_type: str

    @property
    def extension(self) -> str:
        kind = self.content_type.split(";")[0].strip().lower()
        return {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "application/pdf": "pdf",
            "image/svg+xml": "svg",
            "text/html": "html",
        }.get(kind, "bin")


def cabinet_time(moment: datetime) -> str:
    """``2026-11-15T13:05:00+03:00``: seconds, no fraction, an offset."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MOSCOW).replace(microsecond=0).isoformat()


def money(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_phone(phone: str) -> str:
    """Digits only, Russian numbers as 7XXXXXXXXXX."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits


def receipt_url(inn: str, receipt_uuid: str) -> str:
    """The receipt as the tax service shows it; anyone with the link can open it."""
    return f"{BASE_URL}/v1/receipt/{inn}/{receipt_uuid}/print"


def token_expiry(token: str) -> Optional[datetime]:
    """When a JWT says it ends; None if it says nothing (or is no JWT)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = payload.get("exp") if isinstance(payload, dict) else None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    except (ValueError, TypeError, OverflowError):
        return None


def _expires_at(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(f"Unreadable token expiry '{value}' from the tax cabinet")
    return datetime_now() + DEFAULT_TOKEN_TTL


class NalogClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        proxy: Optional[str] = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.timeout = timeout

    # -- login --------------------------------------------------------------

    @staticmethod
    def device_info(device_id: str) -> dict[str, Any]:
        return {
            "sourceType": "WEB",
            "sourceDeviceId": device_id,
            "appVersion": APP_VERSION,
            "metaDetails": {"userAgent": USER_AGENT},
        }

    async def login_password(self, inn: str, password: str, device_id: str) -> NalogSession:
        body = {
            "username": inn,
            "password": password,
            "deviceInfo": self.device_info(device_id),
        }
        data = await self._call("POST", "/v1/auth/lkfl", body, auth_call=True)
        return self._session(data)

    async def sms_start(self, phone: str) -> SmsChallenge:
        body = {"phone": normalize_phone(phone), "requireTpToBeActive": True}
        data = await self._call("POST", "/v2/auth/challenge/sms/start", body, auth_call=True)
        token = data.get("challengeToken")
        if not isinstance(token, str) or not token:
            raise NalogError("No challenge token in the answer", "bad.answer")
        expires_in = data.get("expireIn")
        return SmsChallenge(token=token, expires_in=int(expires_in or 120))

    async def sms_verify(
        self, phone: str, code: str, challenge: str, device_id: str
    ) -> NalogSession:
        body = {
            "phone": normalize_phone(phone),
            # Digits only: "12 34" or "1234." typed by hand is the same code.
            "code": "".join(ch for ch in code if ch.isdigit()),
            "challengeToken": challenge,
            "deviceInfo": self.device_info(device_id),
        }
        data = await self._call("POST", "/v1/auth/challenge/sms/verify", body, auth_call=True)
        return self._session(data)

    async def refresh(self, refresh_token: str, device_id: str) -> NalogSession:
        body = {"deviceInfo": self.device_info(device_id), "refreshToken": refresh_token}
        data = await self._call("POST", "/v1/auth/token", body, auth_call=True)
        # The cabinet may keep the refresh token as it was.
        return self._session(data, refresh_token=refresh_token)

    async def profile(self, token: str) -> dict[str, Any]:
        """The taxpayer the token opens; checks that a token works."""
        async with self._session_http() as http:
            try:
                async with http.get(
                    self.base_url + "/v1/user", headers=self._headers(token)
                ) as response:
                    content = await response.read()
                    if response.status != 200:
                        raise self._error(response.status, content)
                    return self._json(content)
            except NalogError:
                raise
            except (*_NOT_CONNECTED, aiohttp.ClientError, TimeoutError) as e:
                raise NalogUnreachableError(f"{type(e).__name__}: {e}", "network") from e

    # -- incomes ------------------------------------------------------------

    async def create_income(
        self,
        token: str,
        name: str,
        amount: Decimal,
        operation_time: datetime,
        request_time: Optional[datetime] = None,
        client_inn: Optional[str] = None,
        client_name: Optional[str] = None,
    ) -> str:
        """Registers an income -- from a private person, or from the
        organisation ``client_inn`` names; the receipt's uuid."""
        total = money(amount)
        if client_inn:
            if not client_name:
                raise ValueError("An organisation's receipt needs its name")
            client = {
                "contactPhone": None,
                "displayName": client_name,
                "incomeType": "FROM_LEGAL_ENTITY",
                "inn": client_inn,
            }
        else:
            client = {
                "contactPhone": None,
                "displayName": None,
                "incomeType": "FROM_INDIVIDUAL",
                "inn": None,
            }
        body = {
            "operationTime": cabinet_time(operation_time),
            "requestTime": cabinet_time(request_time or datetime_now()),
            "services": [{"name": name, "amount": float(total), "quantity": 1}],
            "totalAmount": f"{total:.2f}",
            "client": client,
            "paymentType": "CASH",
            "ignoreMaxTotalIncomeRestriction": False,
        }
        data = await self._call("POST", "/v1/income", body, token=token, sends_income=True)
        receipt_uuid = data.get("approvedReceiptUuid")
        if not isinstance(receipt_uuid, str) or not receipt_uuid:
            # Went through and came back with no receipt: not clear what happened.
            raise NalogUnknownError("No receipt in the answer", "bad.answer")
        return receipt_uuid

    async def cancel_income(
        self,
        token: str,
        receipt_uuid: str,
        comment: str,
        operation_time: Optional[datetime] = None,
    ) -> None:
        if comment not in (CANCEL_WRONG, CANCEL_REFUND):
            raise ValueError(f"The cabinet takes no cancel comment '{comment}'")
        now = datetime_now()
        body = {
            "operationTime": cabinet_time(operation_time or now),
            "requestTime": cabinet_time(now),
            "comment": comment,
            "receiptUuid": receipt_uuid,
            "partnerCode": None,
        }
        await self._call("POST", "/v1/cancel", body, token=token)

    async def receipt_file(
        self, inn: str, receipt_uuid: str, token: Optional[str] = None
    ) -> ReceiptFile:
        path = f"/v1/receipt/{inn}/{receipt_uuid}/print"
        async with self._session_http() as http:
            try:
                async with http.get(self.base_url + path, headers=self._headers(token)) as response:
                    content = await response.read()
                    if response.status != 200:
                        raise self._error(response.status, content)
                    return ReceiptFile(
                        content=content,
                        content_type=response.headers.get("Content-Type", ""),
                    )
            except NalogError:
                raise
            except (*_NOT_CONNECTED, aiohttp.ClientError, TimeoutError) as e:
                raise NalogUnreachableError(f"{type(e).__name__}: {e}", "network") from e

    # -- plumbing -----------------------------------------------------------

    def _headers(self, token: Optional[str] = None, auth_call: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": SITE_URL,
            "Referer": f"{SITE_URL}/auth/login" if auth_call else f"{SITE_URL}/sales/create",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _session_http(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        if self.proxy:
            connector = ProxyConnector.from_url(self.proxy)
            return aiohttp.ClientSession(connector=connector, timeout=timeout)
        return aiohttp.ClientSession(timeout=timeout)

    async def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        token: Optional[str] = None,
        auth_call: bool = False,
        sends_income: bool = False,
    ) -> dict[str, Any]:
        url = self.base_url + path
        async with self._session_http() as http:
            try:
                async with http.request(
                    method, url, json=body, headers=self._headers(token, auth_call)
                ) as response:
                    content = await response.read()
                    if response.status == 200:
                        return self._json(content, sends_income)
                    raise self._error(response.status, content, sends_income)
            except NalogError:
                raise
            except _NOT_CONNECTED as e:
                # No connection was made: nothing reached the cabinet.
                raise NalogUnreachableError(f"{type(e).__name__}: {e}", "network") from e
            except (aiohttp.ClientError, TimeoutError) as e:
                # Sent, maybe processed, no answer.
                kind = NalogUnknownError if sends_income else NalogUnreachableError
                raise kind(f"{type(e).__name__}: {e}", "network") from e

    @staticmethod
    def _json(content: bytes, sends_income: bool = False) -> dict[str, Any]:
        try:
            data = json.loads(content or b"{}")
        except ValueError as e:
            kind = NalogUnknownError if sends_income else NalogError
            raise kind("Not JSON in the answer", "bad.answer", 200) from e
        if not isinstance(data, dict):
            kind = NalogUnknownError if sends_income else NalogError
            raise kind("Not an object in the answer", "bad.answer", 200)
        return data

    @staticmethod
    def _error(status: int, content: bytes, sends_income: bool = False) -> NalogError:
        code, message = "", ""
        try:
            data = json.loads(content or b"{}")
            if isinstance(data, dict):
                code = str(data.get("code") or "")
                message = str(data.get("message") or "")
        except ValueError:
            message = content[:200].decode("utf-8", "replace")

        if status == 401 or code.startswith("authentication"):
            return NalogAuthError(message or "Unauthorized", code, status)
        if status in _NOT_REACHED_STATUSES:
            return NalogUnreachableError(message or "Service unavailable", code, status)
        if status >= 500 and sends_income:
            return NalogUnknownError(message or "Server error", code, status)
        if status >= 500:
            return NalogUnreachableError(message or "Server error", code, status)
        return NalogError(message or "Request refused", code, status)

    def _session(self, data: dict[str, Any], refresh_token: str = "") -> NalogSession:
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise NalogAuthError("No token in the answer", "bad.answer")
        new_refresh = data.get("refreshToken") or refresh_token
        if not isinstance(new_refresh, str) or not new_refresh:
            raise NalogAuthError("No refresh token in the answer", "bad.answer")
        profile = data.get("profile")
        inn = profile.get("inn") if isinstance(profile, dict) else None
        return NalogSession(
            token=token,
            expires_at=_expires_at(data.get("tokenExpireIn")),
            refresh_token=new_refresh,
            inn=str(inn) if inn else None,
        )
