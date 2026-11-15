"""What an admin does with receipts, and what the windows take from them."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from dishka import Provider, Scope, make_async_container
from dishka.integrations.aiogram import CONTAINER_NAME

import src.infrastructure.services  # noqa: F401  # before the routers, as in the app
from src.application.common import Notifier, TranslatorRunner
from src.application.common.dao import UserDao
from src.application.dto import UserDto
from src.application.dto.s_receipts import NalogAccountDto, ReceiptStatus
from src.core.constants import USER_KEY
from src.core.enums import Role
from src.infrastructure.services.s.nalog import CANCEL_WRONG, NalogAuthError, ReceiptFile
from src.infrastructure.services.s.nalog_auth import NalogAuth, is_payer_inn
from src.infrastructure.services.s.receipts import Receipts, parse_receipt_uuid
from src.telegram.routers.s.dashboard.receipts import INPUT, ITEM, LOGIN_INN, on_input
from src.telegram.routers.s.receipts import on_receipt
from src.telegram.s_states import SDashboard
from tests.test_s_receipts import (
    INN,
    Cabinet,
    FakeReceipts,
    Settings,
    receipt,
    service,
)

ADMIN = UserDto(id=1, telegram_id=1, name="Admin", role=Role.OWNER)


class TestAdminActions:
    async def test_retry_puts_a_failed_receipt_back_and_sends_it(self) -> None:
        dao, cabinet = FakeReceipts(receipt(status=ReceiptStatus.FAILED, attempts=12)), Cabinet()
        receipts, _ = service(dao, cabinet)
        assert await receipts.retry(1)
        assert len(cabinet.incomes) == 1 and dao.rows[1].status == ReceiptStatus.DELIVERED

    async def test_retry_leaves_an_issued_receipt_alone(self) -> None:
        dao, cabinet = FakeReceipts(receipt(status=ReceiptStatus.DELIVERED)), Cabinet()
        receipts, _ = service(dao, cabinet)
        assert not await receipts.retry(1) and not cabinet.incomes

    async def test_a_number_typed_by_hand_goes_to_the_buyer(self) -> None:
        dao = FakeReceipts(receipt(status=ReceiptStatus.UNKNOWN))
        receipts, _ = service(dao)
        receipts.auth.account = _account  # type: ignore[method-assign]
        receipts.get = _getter(dao)  # type: ignore[method-assign]
        link = f"https://lknpd.nalog.ru/api/v1/receipt/{INN}/200ABCdef1/print"
        saved = await receipts.set_manual(1, link)
        assert saved is not None and saved.receipt_uuid == "200abcdef1"
        assert saved.status == ReceiptStatus.DELIVERED

    async def test_a_number_is_not_set_twice(self) -> None:
        dao = FakeReceipts(receipt(status=ReceiptStatus.DELIVERED, receipt_uuid="u1"))
        receipts, _ = service(dao)
        receipts.auth.account = _account  # type: ignore[method-assign]
        assert await receipts.set_manual(1, "200abcdef1") is None
        assert dao.rows[1].receipt_uuid == "u1"

    async def test_reissue_cancels_first_then_makes_out_to_the_organisation(self) -> None:
        issued = receipt(status=ReceiptStatus.DELIVERED, receipt_uuid="u1", inn=INN)
        dao, cabinet = FakeReceipts(issued), Cabinet(income="u2")
        receipts, _ = service(dao, cabinet)
        receipts.get = _getter(dao)  # type: ignore[method-assign]
        await receipts.reissue_to_organisation(1, "7707083893", "ООО Ромашка")
        assert cabinet.canceled == [("u1", CANCEL_WRONG)]
        row = dao.rows[1]
        assert (row.receipt_uuid, row.client_inn, row.status) == (
            "u2",
            "7707083893",
            ReceiptStatus.DELIVERED,
        )

    async def test_a_refused_cancel_changes_nothing(self) -> None:
        issued = receipt(status=ReceiptStatus.DELIVERED, receipt_uuid="u1", inn=INN)
        dao, cabinet = FakeReceipts(issued), Cabinet(cancel=NalogAuthError("no"))
        receipts, _ = service(dao, cabinet)
        receipts.get = _getter(dao)  # type: ignore[method-assign]
        with pytest.raises(NalogAuthError):
            await receipts.reissue_to_organisation(1, "7707083893", "ООО Ромашка")
        assert dao.rows[1] == issued and not cabinet.incomes

    async def test_earlier_payments_stop_at_the_switch_on(self) -> None:
        dao = FakeReceipts()
        calls: list[tuple[datetime, Optional[datetime]]] = []

        async def enqueue(since: datetime, until: Optional[datetime] = None) -> int:
            calls.append((since, until))
            return 0

        dao.enqueue_paid = enqueue  # type: ignore[method-assign]
        receipts, _ = service(dao)
        day = datetime(2026, 11, 1, tzinfo=timezone.utc)
        assert await receipts.add_since(day) == 0
        settings = Settings()
        assert calls == [(day, datetime.fromisoformat(settings.value.extra.s.receipts.since))]


async def _account() -> NalogAccountDto:
    return NalogAccountDto(inn=INN)


def _getter(dao: FakeReceipts) -> Any:
    async def get(receipt_id: int) -> Any:
        return dao.rows.get(receipt_id)

    return get


def test_payer_inns_and_receipt_numbers() -> None:
    assert is_payer_inn("7707083893") and is_payer_inn(INN)
    assert not is_payer_inn("7707083894") and not is_payer_inn("123")
    assert parse_receipt_uuid(" 200abc123x ") == "200abc123x"
    assert parse_receipt_uuid("нет") is None and parse_receipt_uuid("a/b c") is None


# -- the input window ---------------------------------------------------------


class Notices:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify_user(self, user: Any, payload: Any = None, i18n_key: str = "") -> None:
        self.sent.append(payload.i18n_key if payload else i18n_key)


class FakeAuth:
    def __init__(self, fails: Optional[Exception] = None) -> None:
        self.fails = fails
        self.logins: list[tuple[str, ...]] = []
        self.proxy: Any = "unset"

    async def login_password(self, inn: str, password: str) -> None:
        if self.fails:
            raise self.fails
        self.logins.append(("password", inn, password))

    async def sms_start(self, phone: str) -> Any:
        return SimpleNamespace(token="C1", expires_in=120)

    async def sms_verify(self, inn: str, phone: str, code: str, challenge: str) -> None:
        self.logins.append(("sms", inn, phone, code, challenge))

    async def set_proxy(self, proxy: Optional[str]) -> None:
        self.proxy = proxy


class Users:
    async def get_by_telegram_id(self, telegram_id: int) -> Optional[UserDto]:
        return UserDto(id=7, telegram_id=telegram_id, name="B") if telegram_id == 700 else None


class Manager:
    def __init__(self, deps: dict[type, Any], **data: Any) -> None:
        provider = Provider(scope=Scope.APP)
        for kind in deps:
            provider.from_context(provides=kind, scope=Scope.APP)
        self.middleware_data = {
            CONTAINER_NAME: make_async_container(provider, context=deps),
            USER_KEY: ADMIN,
        }
        self.dialog_data: dict[str, Any] = dict(data)
        self.show_mode: Any = None
        self.state: Any = None

    async def switch_to(self, state: Any, **kwargs: Any) -> None:
        self.state = state


class Message:
    def __init__(self, text: str) -> None:
        self.text = text
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


async def type_in(
    text: str, auth: Optional[FakeAuth] = None, receipts: Any = None, **data: Any
) -> tuple[Manager, Notices, FakeAuth, Message]:
    notices, auth = Notices(), auth or FakeAuth()
    manager = Manager(
        {
            NalogAuth: auth,
            Receipts: receipts or SimpleNamespace(),
            UserDao: Users(),
            Notifier: notices,
        },
        **data,
    )
    message = Message(text)
    await on_input(message, None, manager)  # type: ignore[arg-type]
    return manager, notices, auth, message


class TestInput:
    async def test_login_by_password_in_two_steps_and_the_password_is_deleted(self) -> None:
        manager, _, auth, message = await type_in(
            INN, **{INPUT: "inn", "s_receipts_login": "password"}
        )
        assert manager.dialog_data[INPUT] == "password" and not message.deleted
        data = manager.dialog_data
        manager, notices, auth, message = await type_in("secret", auth, **data)
        assert auth.logins == [("password", INN, "secret")] and message.deleted
        assert manager.state == SDashboard.RECEIPTS
        assert notices.sent == ["ntf-s-receipts.logged-in"]

    async def test_a_wrong_password_is_told_and_still_deleted(self) -> None:
        manager, notices, _, message = await type_in(
            "bad",
            FakeAuth(fails=NalogAuthError("Неверный <пароль>")),
            **{INPUT: "password", LOGIN_INN: INN},
        )
        assert message.deleted and notices.sent == ["ntf-s-receipts.cabinet"]
        assert manager.state is None

    async def test_login_by_sms(self) -> None:
        manager, _, auth, _ = await type_in("+79001234567", **{INPUT: "phone", LOGIN_INN: INN})
        assert manager.dialog_data[INPUT] == "code"
        manager, notices, auth, message = await type_in("1234", auth, **manager.dialog_data)
        assert auth.logins == [("sms", INN, "+79001234567", "1234", "C1")] and message.deleted
        assert "s_receipts_challenge" not in manager.dialog_data

    @pytest.mark.parametrize(
        "kind, text, notice",
        [
            ("inn", "12345", "ntf-s-receipts.inn-invalid"),
            ("proxy", "ftp://x", "ntf-s-receipts.proxy-invalid"),
            ("since", "2026-11-01", "ntf-s-receipts.date-invalid"),
            ("org", "1234567890 ООО", "ntf-s-receipts.org-invalid"),
            ("org", "7707083893", "ntf-s-receipts.org-invalid"),
            ("find", "999", "ntf-s-receipts.user-not-found"),
        ],
    )
    async def test_what_will_not_do(self, kind: str, text: str, notice: str) -> None:
        manager, notices, _, _ = await type_in(text, **{INPUT: kind, ITEM: 1})
        assert notices.sent == [notice] and manager.state is None

    async def test_the_proxy_is_saved_and_deleted_from_the_chat(self) -> None:
        _, notices, auth, message = await type_in("socks5h://u:p@h:1", **{INPUT: "proxy"})
        assert auth.proxy == "socks5h://u:p@h:1" and message.deleted
        _, _, auth, _ = await type_in("-", **{INPUT: "proxy"})
        assert auth.proxy is None

    async def test_find_opens_the_users_list(self) -> None:
        manager, _, _, _ = await type_in("700", **{INPUT: "find"})
        assert manager.state == SDashboard.RECEIPTS_LIST
        assert manager.dialog_data["s_receipts_list"] == "7"

    async def test_payments_from_a_date(self) -> None:
        days: list[datetime] = []

        async def add_since(day: datetime) -> int:
            days.append(day)
            return 3

        receipts = SimpleNamespace(add_since=add_since)
        _, notices, _, _ = await type_in("01.11.2026", receipts=receipts, **{INPUT: "since"})
        assert notices.sent == ["ntf-s-receipts.added"]
        assert days[0].isoformat() == "2026-11-01T00:00:00+03:00"


# -- the buyer's list ---------------------------------------------------------


class Translator:
    def get(self, key: str, **kw: Any) -> str:
        return key


class Callback:
    def __init__(self) -> None:
        self.documents: list[Any] = []
        self.message = self

    async def answer_document(self, document: Any, caption: str) -> None:
        self.documents.append(document.filename)


async def press(receipt_row: Any, file: Any = None) -> tuple[Callback, Notices]:
    notices, callback = Notices(), Callback()

    async def get(receipt_id: int) -> Any:
        return receipt_row

    async def fetch_file(row: Any) -> Any:
        return file

    receipts = SimpleNamespace(get=get, fetch_file=fetch_file)
    manager = Manager({Receipts: receipts, Notifier: notices, TranslatorRunner: Translator()})
    await on_receipt(callback, None, manager, "1")  # type: ignore[arg-type]
    return callback, notices


class TestMyReceipts:
    async def test_a_receipt_comes_as_a_file(self) -> None:
        row = receipt(user_id=ADMIN.id, receipt_uuid="u1", inn=INN, status=ReceiptStatus.DELIVERED)
        callback, notices = await press(row, ReceiptFile(b"\x89PNG", "image/png"))
        assert callback.documents == ["receipt_2026-11-15_1.png"] and not notices.sent

    async def test_someone_elses_receipt_is_not_given(self) -> None:
        row = receipt(user_id=999, receipt_uuid="u1", inn=INN, status=ReceiptStatus.DELIVERED)
        callback, notices = await press(row, ReceiptFile(b"\x89PNG", "image/png"))
        assert not callback.documents and notices.sent == ["ntf-s-receipts.none"]

    async def test_no_file_gives_the_link(self) -> None:
        row = receipt(user_id=ADMIN.id, receipt_uuid="u1", inn=INN, status=ReceiptStatus.DELIVERED)
        callback, notices = await press(row, None)
        assert not callback.documents and notices.sent == ["ntf-s-receipts.unavailable"]


async def test_without_the_permission_nothing_is_done() -> None:
    notices, auth = Notices(), FakeAuth()
    manager = Manager(
        {NalogAuth: auth, Receipts: SimpleNamespace(), UserDao: Users(), Notifier: notices}
    )
    manager.middleware_data[USER_KEY] = UserDto(id=2, telegram_id=2, name="A", role=Role.ADMIN)
    manager.dialog_data[INPUT] = "proxy"
    await on_input(Message("-"), None, manager)  # type: ignore[arg-type]
    assert auth.proxy == "unset" and not notices.sent
