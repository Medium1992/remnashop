"""Receipts after payments: one per payment, whatever fails on the way."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from dishka import make_container
from sqlalchemy.dialects import postgresql

from src.application.common import TranslatorHub
from src.application.dto import SettingsDto, UserDto
from src.application.dto.s_receipts import ReceiptDto, ReceiptStatus
from src.application.use_cases.s.settings import ToggleReceipts
from src.core.config import AppConfig
from src.core.enums import PurchaseType, Role
from src.core.utils.time import datetime_now
from src.infrastructure.database.dao.s_receipts import ReceiptDao, payment_amount
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.di.providers.retort import RetortProvider
from src.infrastructure.services.s.nalog import (
    CANCEL_REFUND,
    NalogAuthError,
    NalogError,
    NalogForbiddenError,
    NalogUnknownError,
    NalogUnreachableError,
    ReceiptFile,
)
from src.infrastructure.services.s.nalog_auth import (
    NalogLoginLostError,
    NalogNotLoggedInError,
)
from src.infrastructure.services.s.receipts import MAX_ATTEMPTS, Receipts, receipt_term

INN = "500100732259"
BUYER = UserDto(id=7, telegram_id=700, name="Buyer")
PAID = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def receipt(n: int = 1, **kw: Any) -> ReceiptDto:
    values: dict[str, Any] = {
        "id": n,
        "payment_id": uuid4(),
        "user_id": BUYER.id,
        "amount": Decimal("199.00"),
        "paid_at": PAID,
        "purchase_type": PurchaseType.NEW,
        "duration": 30,
        "status": ReceiptStatus.PENDING,
    }
    return ReceiptDto(**{**values, **kw})


class FakeUow:
    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class FakeReceipts:
    """``ReceiptDao`` in memory, with the same rules about status."""

    def __init__(self, *rows: ReceiptDto, refunded: tuple[int, ...] = ()) -> None:
        self.rows = {row.id: row for row in rows}
        self.refunded_ids = set(refunded)
        self.enqueued_since: list[datetime] = []

    async def enqueue_paid(self, since: datetime, until: Optional[datetime] = None) -> int:
        self.enqueued_since.append(since)
        return 0

    async def stuck_sending(self, before: datetime) -> int:
        return 0

    async def due(self, now: datetime, limit: int = 20) -> list[ReceiptDto]:
        return [
            row
            for row in self.rows.values()
            if row.status == ReceiptStatus.PENDING
            and (getattr(row, "next_try_at", None) or now) <= now
        ]

    async def undelivered_fresh(self, limit: int = 20) -> list[ReceiptDto]:
        return [
            row
            for row in self.rows.values()
            if row.status == ReceiptStatus.REGISTERED and row.delivery_error is None
        ]

    async def refunded(self, limit: int = 20) -> list[ReceiptDto]:
        return [
            row
            for row in self.rows.values()
            if row.id in self.refunded_ids
            and row.status in (ReceiptStatus.REGISTERED, ReceiptStatus.DELIVERED)
            and not (row.error or "").startswith("cancel:")
        ]

    async def claim(self, receipt_id: int, name: str) -> bool:
        row = self.rows[receipt_id]
        if row.status != ReceiptStatus.PENDING:
            return False
        self.rows[receipt_id] = replace(
            row, status=ReceiptStatus.SENDING, name=name, attempts=row.attempts + 1
        )
        return True

    async def mark(
        self, receipt_id: int, status: ReceiptStatus, *, expect: Any = None, **values: Any
    ) -> bool:
        row = self.rows[receipt_id]
        if expect is not None and row.status not in expect:
            return False
        next_try = values.pop("next_try_at", None)
        self.rows[receipt_id] = replace(row, status=status, **values)
        object.__setattr__(self.rows[receipt_id], "next_try_at", next_try)
        return True

    async def note(self, receipt_id: int, **values: Any) -> None:
        self.rows[receipt_id] = replace(self.rows[receipt_id], **values)


class Settings:
    def __init__(self, enabled: bool = True) -> None:
        self.value = SettingsDto()
        self.value.extra.s.receipts.enabled = enabled
        self.value.extra.s.receipts.since = PAID.isoformat() if enabled else ""

    async def get(self) -> SettingsDto:
        return self.value


class Users:
    def __init__(self, user: Optional[UserDto] = BUYER) -> None:
        self.user = user

    async def get_by_id(self, user_id: int) -> Optional[UserDto]:
        return self.user


class Cabinet:
    """What the service asks of the client, scripted."""

    def __init__(self, income: Any = "uuid-1", file: Any = None, cancel: Any = None) -> None:
        self.income = income
        self.file = file if file is not None else ReceiptFile(b"\x89PNG", "image/png")
        self.cancel = cancel
        self.incomes: list[tuple[str, Decimal, datetime]] = []
        self.clients: list[tuple[Optional[str], Optional[str]]] = []
        self.canceled: list[tuple[str, str]] = []

    async def create_income(
        self,
        token: str,
        name: str,
        amount: Decimal,
        operation_time: datetime,
        client_inn: Optional[str] = None,
        client_name: Optional[str] = None,
    ) -> str:
        self.incomes.append((name, amount, operation_time))
        self.clients.append((client_inn, client_name))
        if isinstance(self.income, Exception):
            raise self.income
        return str(self.income)

    async def receipt_file(self, inn: str, uuid: str, token: Any = None) -> ReceiptFile:
        if isinstance(self.file, Exception):
            raise self.file
        return self.file  # type: ignore[no-any-return]

    async def cancel_income(self, token: str, uuid: str, comment: str) -> None:
        self.canceled.append((uuid, comment))
        if isinstance(self.cancel, Exception):
            raise self.cancel


class Auth:
    def __init__(self, cabinet: Cabinet, fails: Optional[Exception] = None) -> None:
        self.cabinet = cabinet
        self.fails = fails
        self.forgotten = 0

    async def session(self) -> tuple[Cabinet, str, str]:
        if self.fails:
            raise self.fails
        return self.cabinet, "T", INN

    async def forget_token(self, reason: str = "") -> None:
        self.forgotten += 1


class Bot:
    def __init__(self, fails: Optional[Exception] = None) -> None:
        self.fails = fails
        self.documents: list[tuple[int, str, str]] = []
        self.messages: list[tuple[int, str]] = []

    async def send_document(self, chat_id: int, document: Any, caption: str) -> None:
        if self.fails:
            raise self.fails
        self.documents.append((chat_id, document.filename, caption))

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.fails:
            raise self.fails
        self.messages.append((chat_id, text))


class Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify_admins(self, payload: Any, roles: Any = None) -> None:
        assert roles == [Role.OWNER, Role.DEV]
        self.sent.append((payload.i18n_key, payload.i18n_kwargs))


class Redis:
    def __init__(self, held: bool = False) -> None:
        self.values: dict[str, str] = {"s_receipts_run": "someone"} if held else {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int = 0) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> Optional[str]:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class Translator:
    def get(self, key: str, **kw: Any) -> str:
        if key == "s-receipt-name":
            return f"name:{kw['renew']}:{kw['unit']}:{kw['value']}"
        return f"{key}|" + "|".join(f"{k}={v}" for k, v in sorted(kw.items()))


class Hub:
    def get_translator_by_locale(self, locale: str) -> Translator:
        return Translator()


def service(
    dao: FakeReceipts,
    cabinet: Optional[Cabinet] = None,
    auth: Optional[Auth] = None,
    bot: Optional[Bot] = None,
    settings: Optional[Settings] = None,
    users: Optional[Users] = None,
    redis: Optional[Redis] = None,
) -> tuple[Receipts, Notifier]:
    notifier = Notifier()
    receipts = Receipts(
        FakeUow(),  # type: ignore[arg-type]
        dao,  # type: ignore[arg-type]
        settings or Settings(),  # type: ignore[arg-type]
        users or Users(),  # type: ignore[arg-type]
        auth or Auth(cabinet or Cabinet()),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        Hub(),  # type: ignore[arg-type]
        bot or Bot(),  # type: ignore[arg-type]
        redis or Redis(),  # type: ignore[arg-type]
    )
    return receipts, notifier


def status(dao: FakeReceipts, n: int = 1) -> ReceiptStatus:
    return dao.rows[n].status


class TestIssue:
    async def test_a_payment_gets_a_receipt_and_the_buyer_gets_it(self) -> None:
        dao, cabinet, bot = FakeReceipts(receipt()), Cabinet(), Bot()
        receipts, notifier = service(dao, cabinet, bot=bot)
        report = await receipts.run()
        assert report is not None and (report.registered, report.delivered) == (1, 1)
        assert cabinet.incomes == [("name:0:months:1", Decimal("199.00"), PAID)]
        row = dao.rows[1]
        assert (row.status, row.receipt_uuid, row.inn) == (ReceiptStatus.DELIVERED, "uuid-1", INN)
        ((chat, filename, caption),) = bot.documents
        assert chat == BUYER.telegram_id and filename == "receipt_2026-11-15_1.png"
        assert f"/receipt/{INN}/uuid-1/print" in caption and "amount=199.00" in caption
        assert not notifier.sent

    async def test_a_second_run_sends_nothing_more(self) -> None:
        dao, cabinet = FakeReceipts(receipt()), Cabinet()
        receipts, _ = service(dao, cabinet)
        await receipts.run()
        await receipts.run()
        assert len(cabinet.incomes) == 1

    async def test_a_row_claimed_by_another_run_is_left_alone(self) -> None:
        dao, cabinet = FakeReceipts(receipt(status=ReceiptStatus.SENDING)), Cabinet()
        receipts, _ = service(dao, cabinet)
        await receipts.run()
        assert not cabinet.incomes and status(dao) == ReceiptStatus.SENDING

    async def test_off_does_nothing(self) -> None:
        dao, cabinet = FakeReceipts(receipt()), Cabinet()
        receipts, _ = service(dao, cabinet, settings=Settings(enabled=False))
        assert await receipts.run() is None
        assert not cabinet.incomes and not dao.enqueued_since

    async def test_one_run_at_a_time(self) -> None:
        dao, cabinet = FakeReceipts(receipt()), Cabinet()
        receipts, _ = service(dao, cabinet, redis=Redis(held=True))
        assert await receipts.run() is None and not cabinet.incomes

    async def test_the_lock_is_let_go(self) -> None:
        redis = Redis()
        receipts, _ = service(FakeReceipts(receipt()), redis=redis)
        await receipts.run()
        assert not redis.values

    async def test_payments_are_counted_from_the_switch_on(self) -> None:
        dao = FakeReceipts()
        receipts, _ = service(dao)
        await receipts.run()
        assert dao.enqueued_since == [PAID]


class TestFailures:
    async def test_the_cabinet_away_is_tried_again_later(self) -> None:
        dao = FakeReceipts(receipt())
        receipts, notifier = service(dao, Cabinet(income=NalogUnreachableError("down")))
        await receipts.run()
        row = dao.rows[1]
        assert row.status == ReceiptStatus.PENDING and row.attempts == 1
        assert getattr(row, "next_try_at") > datetime_now()
        assert not notifier.sent

    async def test_after_the_last_try_it_fails_and_admins_hear(self) -> None:
        dao = FakeReceipts(receipt(attempts=MAX_ATTEMPTS - 1))
        receipts, notifier = service(dao, Cabinet(income=NalogUnreachableError("down")))
        await receipts.run()
        assert status(dao) == ReceiptStatus.FAILED
        ((key, kw),) = notifier.sent
        assert key == "event-s-receipts.problems" and kw["count"] == 1 and kw["amount"] == "199.00"

    @pytest.mark.parametrize(
        "error", [NalogUnknownError("timeout"), RuntimeError("bug")], ids=["unknown", "bug"]
    )
    async def test_no_clear_answer_is_never_sent_again(self, error: Exception) -> None:
        dao, cabinet = FakeReceipts(receipt()), Cabinet(income=error)
        receipts, notifier = service(dao, cabinet)
        await receipts.run()
        await receipts.run()
        assert status(dao) == ReceiptStatus.UNKNOWN and len(cabinet.incomes) == 1
        assert notifier.sent[0][0] == "event-s-receipts.problems"

    async def test_a_refusal_fails_at_once_with_the_reason(self) -> None:
        dao = FakeReceipts(receipt())
        refusal = NalogError("Сумма превышает <лимит>", "validation", 400)
        receipts, notifier = service(dao, Cabinet(income=refusal))
        await receipts.run()
        assert status(dao) == ReceiptStatus.FAILED and "лимит" in (dao.rows[1].error or "")
        assert "&lt;лимит&gt;" in notifier.sent[0][1]["lines"]

    async def test_a_refused_token_is_forgotten_and_the_row_waits(self) -> None:
        dao, auth = FakeReceipts(receipt()), Auth(Cabinet(income=NalogAuthError("401")))
        receipts, _ = service(dao, auth=auth)
        await receipts.run()
        assert status(dao) == ReceiptStatus.PENDING and dao.rows[1].attempts == 0
        assert auth.forgotten == 1

    async def test_a_lost_login_is_told_and_nothing_is_sent(self) -> None:
        dao, cabinet = FakeReceipts(receipt()), Cabinet()
        auth = Auth(cabinet, fails=NalogLoginLostError("expired"))
        receipts, notifier = service(dao, auth=auth)
        await receipts.run()
        assert notifier.sent[0][0] == "event-s-receipts.login-lost"
        assert not cabinet.incomes and status(dao) == ReceiptStatus.PENDING

    async def test_no_login_waits_quietly(self) -> None:
        dao = FakeReceipts(receipt())
        auth = Auth(Cabinet(), fails=NalogNotLoggedInError("none"))
        receipts, notifier = service(dao, auth=auth)
        await receipts.run()
        assert not notifier.sent and status(dao) == ReceiptStatus.PENDING

    async def test_a_failed_purchase_run_never_raises(self) -> None:
        receipts, _ = service(FakeReceipts(), settings=Settings())
        receipts.settings_dao = None  # type: ignore[assignment]
        await receipts.on_purchase(None)  # type: ignore[arg-type]


class TestDelivery:
    async def test_a_blocked_buyer_stays_undelivered(self) -> None:
        dao = FakeReceipts(receipt())
        blocked = TelegramForbiddenError(None, "bot was blocked")  # type: ignore[arg-type]
        receipts, _ = service(dao, bot=Bot(fails=blocked))
        await receipts.run()
        row = dao.rows[1]
        assert row.status == ReceiptStatus.REGISTERED and "blocked" in (row.delivery_error or "")
        # Not retried by itself: the admin's window has it.
        assert not await dao.undelivered_fresh()

    async def test_telegram_away_is_tried_on_the_next_run(self) -> None:
        dao, bot = FakeReceipts(receipt()), Bot(fails=TelegramNetworkError(None, "x"))  # type: ignore[arg-type]
        receipts, _ = service(dao, bot=bot)
        await receipts.run()
        assert status(dao) == ReceiptStatus.REGISTERED and dao.rows[1].delivery_error is None
        bot.fails = None
        await receipts.run()
        assert status(dao) == ReceiptStatus.DELIVERED and len(bot.documents) == 1

    @pytest.mark.parametrize(
        "file",
        [NalogUnreachableError("x"), ReceiptFile(b"<html>", "text/html")],
        ids=["no-file", "not-a-picture"],
    )
    async def test_without_a_picture_the_link_goes(self, file: Any) -> None:
        dao, bot = FakeReceipts(receipt()), Bot()
        receipts, _ = service(dao, Cabinet(file=file), bot=bot)
        await receipts.run()
        assert not bot.documents and "msg-s-receipt.sent" in bot.messages[0][1]
        assert status(dao) == ReceiptStatus.DELIVERED

    async def test_a_buyer_gone_from_telegram(self) -> None:
        dao = FakeReceipts(receipt())
        receipts, _ = service(dao, users=Users(None))
        await receipts.run()
        assert dao.rows[1].delivery_error == "no telegram"


class TestRefund:
    async def test_a_refunded_payment_loses_its_receipt(self) -> None:
        row = receipt(status=ReceiptStatus.DELIVERED, receipt_uuid="u9", inn=INN)
        dao, cabinet = FakeReceipts(row, refunded=(1,)), Cabinet()
        receipts, _ = service(dao, cabinet)
        await receipts.run()
        assert cabinet.canceled == [("u9", CANCEL_REFUND)]
        assert status(dao) == ReceiptStatus.CANCELED

    async def test_a_refused_cancel_is_not_tried_forever(self) -> None:
        row = receipt(status=ReceiptStatus.DELIVERED, receipt_uuid="u9", inn=INN)
        dao = FakeReceipts(row, refunded=(1,))
        cabinet = Cabinet(cancel=NalogError("already"))
        receipts, _ = service(dao, cabinet)
        await receipts.run()
        await receipts.run()
        assert len(cabinet.canceled) == 1 and status(dao) == ReceiptStatus.DELIVERED


# -- the name in the receipt ----------------------------------------------------


@pytest.fixture(scope="module")
def i18n() -> Any:
    container = make_container(
        RetortProvider(), I18nProvider(), context={AppConfig: AppConfig.get()}, skip_validation=True
    )
    return container.get(TranslatorHub)


@pytest.mark.parametrize(
    "kind, days, name",
    [
        (PurchaseType.NEW, 30, "Подписка на 1 месяц"),
        (PurchaseType.RENEW, 90, "Продление подписки на 3 месяца"),
        (PurchaseType.RENEW, 150, "Продление подписки на 5 месяцев"),
        (PurchaseType.CHANGE, 365, "Подписка на 1 год"),
        (PurchaseType.NEW, 730, "Подписка на 2 года"),
        (PurchaseType.NEW, 21, "Подписка на 21 день"),
        (PurchaseType.RENEW, 45, "Продление подписки на 45 дней"),
        (PurchaseType.NEW, 3, "Подписка на 3 дня"),
        (PurchaseType.NEW, 0, "Подписка"),
    ],
)
def test_the_receipt_says_what_and_for_how_long(
    i18n: Any, kind: PurchaseType, days: int, name: str
) -> None:
    receipts, _ = service(FakeReceipts())
    receipts.translator_hub = i18n
    assert receipts.receipt_name(receipt(purchase_type=kind, duration=days)) == name


def test_terms() -> None:
    assert receipt_term(360) == ("months", 12)
    assert receipt_term(-1) == ("none", 0)


def test_the_admin_texts_render(i18n: Any) -> None:
    ru = i18n.get_translator_by_locale("ru")
    problems = ru.get("event-s-receipts.problems", count=2, amount="398.00", lines="• x")
    assert "2 без чека на 398.00 ₽" in problems
    assert "Войдите заново" in ru.get("event-s-receipts.login-lost", reason="r")
    sent = ru.get("msg-s-receipt.sent", name="Подписка на 1 месяц", amount="199.00", url="u")
    assert "Подписка на 1 месяц" in sent and 'href="u"' in sent


async def test_switching_on_marks_the_moment() -> None:
    settings = Settings(enabled=False)
    use_case = ToggleReceipts(FakeUow(), settings)  # type: ignore[arg-type]
    saved: list[SettingsDto] = []

    async def update(value: SettingsDto) -> None:
        saved.append(value)

    settings.update = update  # type: ignore[attr-defined]
    assert await use_case._execute(BUYER, None) is True
    since = datetime.fromisoformat(settings.value.extra.s.receipts.since)
    assert abs(since - datetime_now()) < timedelta(seconds=5)
    assert await use_case._execute(BUYER, None) is False
    assert settings.value.extra.s.receipts.since == ""


# -- the SQL the queue runs on -------------------------------------------------


class Captured:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def scalars(self, stmt: Any) -> list[Any]:
        self.statements.append(stmt)
        return []

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)

        class Result:
            def first(self) -> Any:
                return (1,)

            def all(self) -> list[Any]:
                return []

        return Result()

    def sql(self, n: int = 0) -> str:
        dialect: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
        return str(
            self.statements[n].compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        )


async def test_only_real_rouble_payments_are_queued() -> None:
    session = Captured()
    await ReceiptDao(session).enqueue_paid(PAID)  # type: ignore[arg-type]
    sql = session.sql()
    assert "transactions.status = 'COMPLETED'" in sql
    assert "transactions.is_test IS false" in sql
    assert "transactions.currency = 'RUB'" in sql
    assert "transactions.updated_at >= '2026-11-15 10:00:00+00:00'" in sql
    assert "transactions.payment_id NOT IN (SELECT s_receipts.payment_id" in sql


async def test_a_claim_takes_only_a_waiting_row() -> None:
    session = Captured()
    assert await ReceiptDao(session).claim(5, "x")  # type: ignore[arg-type]
    sql = session.sql()
    assert "WHERE s_receipts.id = 5 AND s_receipts.status = 'pending'" in sql
    assert "status='sending'" in sql and "attempts=(s_receipts.attempts + 1)" in sql


async def test_a_row_left_sending_goes_to_the_admin_not_the_queue() -> None:
    session = Captured()
    await ReceiptDao(session).stuck_sending(PAID)  # type: ignore[arg-type]
    sql = session.sql()
    assert "s_receipts.status = 'sending'" in sql and "status='unknown'" in sql


def test_the_amount_paid() -> None:
    assert payment_amount({"final_amount": "199.00"}) == Decimal("199.00")
    assert payment_amount({"final_amount": 99.5}) == Decimal("99.5")
    assert payment_amount({}) == 0


async def test_a_refused_access_key_tells_the_admins() -> None:
    class KeyAuth(Auth):
        async def forget_token(self, reason: str = "") -> None:
            raise NalogLoginLostError("the access key was refused")

    dao = FakeReceipts(receipt())
    receipts, notifier = service(dao, auth=KeyAuth(Cabinet(income=NalogAuthError("401"))))
    await receipts.run()
    assert status(dao) == ReceiptStatus.PENDING
    assert notifier.sent[0][0] == "event-s-receipts.login-lost"


async def test_no_right_to_register_waits_and_tells_once() -> None:
    class KeyAuth(Auth):
        async def forget_token(self, reason: str = "") -> None:
            raise NalogLoginLostError(f"the access key was refused: {reason}")

    dao = FakeReceipts(receipt())
    forbidden = NalogForbiddenError("Нет прав", "access.denied", 403)
    receipts, notifier = service(dao, auth=KeyAuth(Cabinet(income=forbidden)))
    await receipts.run()
    assert status(dao) == ReceiptStatus.PENDING and dao.rows[1].attempts == 0
    assert [key for key, _ in notifier.sent] == ["event-s-receipts.login-lost"]
