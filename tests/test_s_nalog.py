"""The "Мой налог" client and the bot's login, against a fake cabinet over HTTP."""

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from src.application.dto.s_receipts import NalogAccountDto, NalogLogin
from src.core.utils.time import datetime_now
from src.infrastructure.database.dao.s_receipts import NalogAccountDao
from src.infrastructure.services.cryptography import CryptographerImpl
from src.infrastructure.services.s.nalog import (
    CANCEL_REFUND,
    NalogAuthError,
    NalogClient,
    NalogError,
    NalogUnknownError,
    NalogUnreachableError,
    cabinet_time,
    normalize_phone,
    receipt_url,
    token_expiry,
)
from src.infrastructure.services.s.nalog_auth import (
    NalogAuth,
    NalogLoginLostError,
    NalogNotLoggedInError,
    is_inn,
    normalize_proxy,
)

INN = "500100732259"  # a valid checksum
LATER = (datetime_now() + timedelta(hours=1)).isoformat()


class Cabinet:
    """A fake lknpd: records requests, answers what a test set."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.answers: dict[str, list[Callable[[], web.StreamResponse]]] = {}

    def answer(self, path: str, status: int = 200, body: Any = None, **kw: Any) -> None:
        def make() -> web.StreamResponse:
            if "raw" in kw:
                return web.Response(status=status, body=kw["raw"], content_type=kw["type"])
            return web.json_response(body if body is not None else {}, status=status)

        self.answers.setdefault(path, []).append(make)

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.requests]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else {}
        self.requests.append((request.path, body, dict(request.headers)))
        queue = self.answers.get(request.path)
        if not queue:
            return web.json_response({"code": "not.set", "message": "?"}, status=404)
        return queue.pop(0)() if len(queue) > 1 else queue[0]()


@pytest.fixture
async def cabinet() -> AsyncIterator[tuple[Cabinet, NalogClient]]:
    fake = Cabinet()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, NalogClient(base_url=str(server.make_url("/api")), timeout=5)
    finally:
        await server.close()


def session_answer(token: str = "T1", refresh: Optional[str] = "R1") -> dict[str, Any]:
    body: dict[str, Any] = {"token": token, "tokenExpireIn": LATER, "profile": {"inn": INN}}
    if refresh:
        body["refreshToken"] = refresh
    return body


class TestClient:
    async def test_password_login(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/auth/lkfl", body=session_answer())
        session = await client.login_password(INN, "secret", "dev1")
        assert (session.token, session.refresh_token, session.inn) == ("T1", "R1", INN)
        assert session.expires_at > datetime_now()
        path, body, headers = fake.requests[0]
        assert body["username"] == INN and body["password"] == "secret"
        assert body["deviceInfo"]["sourceDeviceId"] == "dev1"
        assert body["deviceInfo"]["sourceType"] == "WEB"
        assert "Authorization" not in headers

    async def test_a_wrong_password_is_a_login_error(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, client = cabinet
        fake.answer(
            "/api/v1/auth/lkfl",
            status=400,
            body={"code": "authentication.failed", "message": "Неверный пароль"},
        )
        with pytest.raises(NalogAuthError, match="Неверный пароль"):
            await client.login_password(INN, "bad", "dev1")

    async def test_sms_login(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, client = cabinet
        fake.answer(
            "/api/v2/auth/challenge/sms/start",
            body={"challengeToken": "C1", "expireIn": 120},
        )
        fake.answer("/api/v1/auth/challenge/sms/verify", body=session_answer())
        challenge = await client.sms_start("8 (900) 123-45-67")
        assert challenge.token == "C1"
        await client.sms_verify("+7 900 123-45-67", " 12 34 ", challenge.token, "dev1")
        (_, start, _), (_, verify, _) = fake.requests
        assert start == {"phone": "79001234567", "requireTpToBeActive": True}
        assert verify["phone"] == "79001234567" and verify["code"] == "1234"
        assert verify["challengeToken"] == "C1"

    async def test_refresh_keeps_the_refresh_token_when_none_comes(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/auth/token", body=session_answer("T2", refresh=None))
        session = await client.refresh("R1", "dev1")
        assert (session.token, session.refresh_token) == ("T2", "R1")
        assert fake.requests[0][1] == {
            "deviceInfo": NalogClient.device_info("dev1"),
            "refreshToken": "R1",
        }

    async def test_income(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/income", body={"approvedReceiptUuid": "200abc"})
        paid = datetime(2026, 11, 15, 10, 5, 7, 123456, tzinfo=timezone.utc)
        uuid = await client.create_income("T1", "Подписка на 1 месяц", Decimal("199.5"), paid)
        assert uuid == "200abc"
        _, body, headers = fake.requests[0]
        assert headers["Authorization"] == "Bearer T1"
        assert body["operationTime"] == "2026-11-15T13:05:07+03:00"
        assert body["services"] == [{"name": "Подписка на 1 месяц", "amount": 199.5, "quantity": 1}]
        assert body["totalAmount"] == "199.50"
        assert body["client"]["incomeType"] == "FROM_INDIVIDUAL"
        assert body["paymentType"] == "CASH"
        assert body["ignoreMaxTotalIncomeRestriction"] is False

    async def test_income_from_an_organisation(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/income", body={"approvedReceiptUuid": "200org"})
        await client.create_income(
            "T1", "x", Decimal(1), datetime_now(), client_inn="7707083893", client_name="ООО Р"
        )
        assert fake.requests[0][1]["client"] == {
            "contactPhone": None,
            "displayName": "ООО Р",
            "incomeType": "FROM_LEGAL_ENTITY",
            "inn": "7707083893",
        }
        with pytest.raises(ValueError):
            await client.create_income("T1", "x", Decimal(1), datetime_now(), client_inn="1")

    @pytest.mark.parametrize("status", [500, 504])
    async def test_a_server_error_on_an_income_is_unknown(
        self, cabinet: tuple[Cabinet, NalogClient], status: int
    ) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/income", status=status, body={"message": "oops"})
        with pytest.raises(NalogUnknownError):
            await client.create_income("T1", "x", Decimal(1), datetime_now())

    @pytest.mark.parametrize("status", [502, 503, 429])
    async def test_a_gateway_turning_an_income_away_is_safe_to_retry(
        self, cabinet: tuple[Cabinet, NalogClient], status: int
    ) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/income", status=status)
        with pytest.raises(NalogUnreachableError):
            await client.create_income("T1", "x", Decimal(1), datetime_now())

    async def test_an_answer_with_no_receipt_is_unknown(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/income", body={})
        with pytest.raises(NalogUnknownError):
            await client.create_income("T1", "x", Decimal(1), datetime_now())

    async def test_a_refused_income_says_why(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, client = cabinet
        fake.answer(
            "/api/v1/income",
            status=400,
            body={"code": "validation.failed", "message": "Сумма превышает лимит"},
        )
        with pytest.raises(NalogError, match="лимит") as caught:
            await client.create_income("T1", "x", Decimal(1), datetime_now())
        assert type(caught.value) is NalogError

    async def test_an_expired_token_is_a_login_error(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/income", status=401)
        with pytest.raises(NalogAuthError):
            await client.create_income("T1", "x", Decimal(1), datetime_now())

    async def test_cancel(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, client = cabinet
        fake.answer("/api/v1/cancel", body={"incomeInfo": {}})
        await client.cancel_income("T1", "200abc", CANCEL_REFUND)
        body = fake.requests[0][1]
        assert body["receiptUuid"] == "200abc" and body["comment"] == "Возврат средств"
        assert body["partnerCode"] is None

    async def test_cancel_takes_only_the_cabinets_wording(self) -> None:
        with pytest.raises(ValueError):
            await NalogClient().cancel_income("T1", "u", "просто так")

    async def test_receipt_file(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, client = cabinet
        fake.answer(f"/api/v1/receipt/{INN}/200abc/print", raw=b"\x89PNG", type="image/png")
        receipt = await client.receipt_file(INN, "200abc")
        assert receipt.content == b"\x89PNG" and receipt.extension == "png"

    async def test_nothing_listening_is_unreachable(self) -> None:
        client = NalogClient(base_url="http://127.0.0.1:9/api", timeout=2)
        with pytest.raises(NalogUnreachableError):
            await client.create_income("T1", "x", Decimal(1), datetime_now())


def test_helpers() -> None:
    assert normalize_phone("8 900 123-45-67") == "79001234567"
    assert normalize_phone("9001234567") == "79001234567"
    assert (
        cabinet_time(datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc))
        == "2026-01-02T00:00:00+03:00"
    )
    assert receipt_url(INN, "u1").endswith(f"/api/v1/receipt/{INN}/u1/print")
    assert is_inn(INN) and not is_inn("500100732258") and not is_inn("7707083893")
    assert normalize_proxy(" socks5h://u:p@h:1 ") == "socks5://u:p@h:1"


# -- the login kept by the bot -------------------------------------------------


class FakeUow:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class FakeAccounts:
    def __init__(self, account: Optional[NalogAccountDto] = None) -> None:
        self.account = account or NalogAccountDto()
        self.locks = 0

    async def get(self, lock: bool = False) -> NalogAccountDto:
        self.locks += lock
        return self.account

    async def save(self, account: NalogAccountDto) -> None:
        self.account = account


def logged_in(**kw: Any) -> NalogAccountDto:
    values: dict[str, Any] = {
        "inn": INN,
        "method": NalogLogin.PASSWORD,
        "password": "secret",
        "refresh_token": "R1",
        "token": "T0",
        "token_expires_at": datetime_now() - timedelta(minutes=1),
        "device_id": "dev1",
    }
    return NalogAccountDto(**{**values, **kw})


def auth(cabinet: tuple[Cabinet, NalogClient], account: NalogAccountDto) -> NalogAuth:
    service = NalogAuth(FakeUow(), FakeAccounts(account))  # type: ignore[arg-type]
    service.make_client = lambda proxy: cabinet[1]
    return service


def stored(service: NalogAuth) -> NalogAccountDto:
    return service.account_dao.account  # type: ignore[attr-defined, no-any-return]


class TestAuth:
    async def test_a_fresh_token_is_used_as_it_is(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        service = auth(cabinet, logged_in(token_expires_at=datetime_now() + timedelta(hours=1)))
        _, token, inn = await service.session()
        assert (token, inn) == ("T0", INN) and not fake.requests

    async def test_an_old_token_is_refreshed_under_a_lock(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/token", body=session_answer("T2", "R2"))
        service = auth(cabinet, logged_in())
        _, token, _ = await service.session()
        assert token == "T2" and stored(service).refresh_token == "R2"
        assert service.account_dao.locks == 1  # type: ignore[attr-defined]

    async def test_a_refused_refresh_logs_in_by_password_again(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/token", status=401)
        fake.answer("/api/v1/auth/lkfl", body=session_answer("T3", "R3"))
        service = auth(cabinet, logged_in())
        _, token, _ = await service.session()
        assert token == "T3" and fake.paths() == ["/api/v1/auth/token", "/api/v1/auth/lkfl"]
        assert fake.requests[1][1]["deviceInfo"]["sourceDeviceId"] == "dev1"

    async def test_an_sms_login_is_lost_once_and_then_stays_lost(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/token", status=401, body={"message": "expired"})
        service = auth(cabinet, logged_in(method=NalogLogin.SMS, password=None))
        with pytest.raises(NalogLoginLostError):
            await service.session()
        assert stored(service).lost_at is not None and stored(service).token is None
        with pytest.raises(NalogNotLoggedInError) as again:
            await service.session()
        assert type(again.value) is NalogNotLoggedInError
        assert fake.paths() == ["/api/v1/auth/token"]

    async def test_a_cabinet_away_leaves_the_login_as_it_was(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/token", status=503)
        service = auth(cabinet, logged_in())
        with pytest.raises(NalogUnreachableError):
            await service.session()
        assert stored(service).lost_at is None and stored(service).refresh_token == "R1"

    async def test_no_login(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        with pytest.raises(NalogNotLoggedInError):
            await auth(cabinet, NalogAccountDto()).session()

    async def test_login_by_password_keeps_the_device(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/lkfl", body=session_answer())
        service = auth(cabinet, NalogAccountDto(device_id="dev9", lost_at=datetime_now()))
        account = await service.login_password(INN, "secret")
        assert account.logged_in and account.device_id == "dev9"
        assert stored(service).password == "secret"

    async def test_login_by_sms_for_another_inn_is_refused(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/challenge/sms/verify", body=session_answer())
        service = auth(cabinet, NalogAccountDto())
        with pytest.raises(NalogAuthError):
            await service.sms_verify("772771004120", "79001234567", "1234", "C1")
        assert not stored(service).logged_in

    async def test_logout_forgets_all_but_the_device(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        service = auth(cabinet, logged_in(proxy="socks5://h:1"))
        await service.logout()
        assert stored(service) == NalogAccountDto(device_id="dev1", proxy="socks5://h:1")


# -- what lands in the database ------------------------------------------------


class Captured:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> None:
        self.statements.append(stmt)


def crypto() -> CryptographerImpl:
    config: Any = type("C", (), {"crypt_key": SecretStr(Fernet.generate_key().decode())})()
    return CryptographerImpl(config)


async def test_secrets_are_stored_encrypted() -> None:
    session, cryptographer = Captured(), crypto()
    dao = NalogAccountDao(session, cryptographer)  # type: ignore[arg-type]
    account = logged_in(phone="79001234567", proxy="socks5://u:p@h:1")
    await dao.save(account)
    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    stored_text = json.dumps({k: str(v) for k, v in params.items()})
    for secret in ("secret", "R1", "T0", "79001234567", "u:p@h"):
        assert secret not in stored_text
    assert params["inn"] == INN
    assert cryptographer.decrypt(params["password"]) == "secret"
    assert dao._open(dao._close("R1")) == "R1" and dao._close(None) is None


def test_secrets_stay_out_of_the_repr() -> None:
    text = repr(replace(logged_in(), phone="79001234567", proxy="socks5://u:p@h:1"))
    for secret in ("secret", "R1", "T0", "79001234567", "u:p@h"):
        assert secret not in text


class TestBrowserLogin:
    async def test_a_session_from_a_browser_is_checked_at_once(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/token", body=session_answer("T5", "R5"))
        service = auth(cabinet, NalogAccountDto())
        account = await service.login_browser(INN, "browserdev", "R-from-browser")
        assert account.logged_in and account.method == NalogLogin.BROWSER
        assert stored(service).device_id == "browserdev"
        assert stored(service).refresh_token == "R5"
        assert fake.requests[0][1]["refreshToken"] == "R-from-browser"
        assert fake.requests[0][1]["deviceInfo"]["sourceDeviceId"] == "browserdev"

    async def test_a_dead_session_is_refused_and_nothing_is_saved(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/token", status=401)
        service = auth(cabinet, NalogAccountDto())
        with pytest.raises(NalogAuthError):
            await service.login_browser(INN, "browserdev", "R-old")
        assert not stored(service).logged_in

    async def test_a_browser_login_is_lost_not_logged_in_again(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/auth/token", status=401)
        service = auth(cabinet, logged_in(method=NalogLogin.BROWSER, password=None))
        with pytest.raises(NalogLoginLostError):
            await service.session()
        assert fake.paths() == ["/api/v1/auth/token"]


def jwt(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


class TestAccessKey:
    async def test_a_key_is_checked_and_kept(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/user", body={"inn": INN, "displayName": "Иван"})
        service = auth(cabinet, NalogAccountDto(device_id="dev1"))
        key = jwt({"sub": "x"})
        account = await service.login_key(INN, key)
        assert account.logged_in and account.method == NalogLogin.KEY
        assert account.token_expires_at is None and account.refresh_token is None
        assert fake.requests[0][2]["Authorization"] == f"Bearer {key}"
        _, token, inn = await service.session()
        assert (token, inn) == (key, INN) and len(fake.requests) == 1

    async def test_a_refused_key_is_not_saved(self, cabinet: tuple[Cabinet, NalogClient]) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/user", status=401)
        service = auth(cabinet, NalogAccountDto())
        with pytest.raises(NalogAuthError):
            await service.login_key(INN, jwt({}))
        assert not stored(service).logged_in

    async def test_a_key_of_another_taxpayer_is_refused(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        fake, _ = cabinet
        fake.answer("/api/v1/user", body={"inn": "772771004120"})
        with pytest.raises(NalogAuthError):
            await auth(cabinet, NalogAccountDto()).login_key(INN, jwt({}))

    async def test_no_profile_where_looked_does_not_refuse_the_key(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        service = auth(cabinet, NalogAccountDto())
        assert (await service.login_key(INN, jwt({}))).logged_in

    async def test_an_expired_key_loses_the_login(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        old = logged_in(
            method=NalogLogin.KEY,
            refresh_token=None,
            token=jwt({}),
            token_expires_at=datetime_now() - timedelta(seconds=1),
        )
        service = auth(cabinet, old)
        with pytest.raises(NalogLoginLostError):
            await service.session()
        assert not stored(service).logged_in and stored(service).token == old.token

    async def test_a_key_refused_later_loses_the_login_once(
        self, cabinet: tuple[Cabinet, NalogClient]
    ) -> None:
        key = logged_in(method=NalogLogin.KEY, refresh_token=None, token=jwt({}))
        service = auth(cabinet, replace(key, token_expires_at=None))
        with pytest.raises(NalogLoginLostError):
            await service.forget_token("401")
        await service.forget_token("401")  # already lost: quiet
        assert "refused" in (stored(service).lost_reason or "")


def test_key_expiry_is_read_from_the_jwt() -> None:
    assert token_expiry(jwt({"exp": 2000000000})) == datetime.fromtimestamp(
        2000000000, tz=timezone.utc
    )
    assert token_expiry(jwt({})) is None and token_expiry("not-a-jwt") is None
