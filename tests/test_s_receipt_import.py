"""Receipts made by hand in the cabinet, tied to the bot's payments."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from src.infrastructure.services.s.nalog import MOSCOW, CabinetIncome, NalogClient
from src.infrastructure.services.s.receipt_import import match, usable
from tests.test_s_nalog import Cabinet as FakeCabinet
from tests.test_s_nalog import cabinet  # noqa: F401  # the fixture
from tests.test_s_receipts import FakeReceipts, service

DAY = datetime(2026, 11, 15, tzinfo=MOSCOW)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second)


def payment(amount: int, paid: datetime, user_id: int = 7) -> dict[str, Any]:
    return {
        "payment_id": uuid4(),
        "user_id": user_id,
        "amount": Decimal(amount),
        "paid_at": paid,
        "purchase_type": "NEW",
        "duration": 30,
    }


def income(
    amount: int,
    made: datetime,
    uuid: str = "",
    individual: bool = True,
    canceled: bool = False,
) -> CabinetIncome:
    return CabinetIncome(
        receipt_uuid=uuid or f"200{amount}{made:%H%M%S}",
        amount=Decimal(amount),
        operation_time=made,
        name="Покупка подписки на 1 месяц",
        from_individual=individual,
        canceled=canceled,
    )


class TestMatch:
    def test_a_batch_made_later_pairs_in_order(self) -> None:
        # Paid through the morning, receipts made in one go six seconds apart.
        p1, p2, p3 = payment(250, at(9)), payment(250, at(10)), payment(500, at(10, 30))
        i1, i2, i3 = (
            income(250, at(11, 1, 6)),
            income(500, at(11, 1, 12)),
            income(250, at(11, 1, 18)),
        )
        plan = match([p3, p1, p2], [i3, i2, i1])
        assert [(pair.payment, pair.income) for pair in plan.pairs] == [
            (p1, i1),
            (p3, i2),
            (p2, i3),
        ]
        assert not plan.lone_payments and not plan.lone_incomes
        assert plan.paired_amount == 1000

    def test_a_receipt_never_takes_a_later_payment(self) -> None:
        early = income(250, at(9))
        later = payment(250, at(12))
        plan = match([later], [early])
        assert not plan.pairs and plan.lone_incomes == [early] and plan.lone_payments == [later]

    def test_a_few_minutes_of_clock_difference_are_allowed(self) -> None:
        plan = match([payment(250, at(9, 3))], [income(250, at(9))])
        assert len(plan.pairs) == 1

    def test_amounts_never_mix(self) -> None:
        plan = match([payment(300, at(9))], [income(250, at(10))])
        assert not plan.pairs and len(plan.lone_payments) == len(plan.lone_incomes) == 1

    def test_what_is_left_on_each_side(self) -> None:
        payments = [payment(250, at(9)), payment(250, at(9, 30)), payment(150, at(10))]
        plan = match(payments, [income(250, at(11))])
        assert len(plan.pairs) == 1 and plan.pairs[0].payment is payments[0]
        assert plan.lone_payments == [payments[1], payments[2]]
        assert plan.lone_payments_amount == 400


def test_only_receipts_that_can_stand_for_a_payment() -> None:
    good = income(250, at(9), "200a")
    incomes = [
        good,
        income(250, at(9), "200b", individual=False),
        income(250, at(9), "200c", canceled=True),
        income(250, at(9), "200d"),
    ]
    assert usable(incomes, known={"200d"}) == [good]


async def test_the_cabinet_is_read_page_by_page(
    cabinet: tuple[FakeCabinet, NalogClient],  # noqa: F811
) -> None:
    fake, client = cabinet

    def item(uuid: str, made: str, amount: float, **kw: Any) -> dict[str, Any]:
        return {
            "approvedReceiptUuid": uuid,
            "name": "Покупка подписки на 1 месяц",
            "operationTime": made,
            "totalAmount": amount,
            "incomeType": "FROM_INDIVIDUAL",
            "cancellationInfo": None,
            **kw,
        }

    fake.answer(
        "/api/v1/incomes",
        body={"content": [item("200b", "2026-11-15T11:01:12+03:00", 5e2)], "hasMore": True},
    )
    fake.answer(
        "/api/v1/incomes",
        body={
            "content": [
                item("200a", "2026-11-15T11:01:06+03:00", 2.5e2),
                item("200c", "2026-11-15T11:02:00+03:00", 1.5e2, cancellationInfo={"x": 1}),
            ],
            "hasMore": False,
        },
    )
    since = datetime(2026, 11, 1, tzinfo=MOSCOW)
    incomes = await client.list_incomes("T1", since, since + timedelta(days=20), page=1)
    assert [i.receipt_uuid for i in incomes] == ["200a", "200b", "200c"]
    assert incomes[0].amount == Decimal("250.00") and incomes[2].canceled
    assert incomes[0].operation_time == datetime(2026, 11, 15, 8, 1, 6, tzinfo=timezone.utc)
    assert fake.paths() == ["/api/v1/incomes", "/api/v1/incomes"]
    assert [r[2].get("Authorization") for r in fake.requests] == ["Bearer T1"] * 2


# -- the service ---------------------------------------------------------------


class ImportCabinet:
    def __init__(self, incomes: list[CabinetIncome]) -> None:
        self.incomes = incomes
        self.registered = 0

    async def list_incomes(self, token: str, since: datetime, until: datetime) -> Any:
        return self.incomes

    async def create_income(self, *args: Any, **kwargs: Any) -> str:
        self.registered += 1
        return "new"


class ImportDao(FakeReceipts):
    def __init__(self, payments: list[dict[str, Any]], known: Optional[set[str]] = None) -> None:
        super().__init__()
        self.payments = payments
        self.known = known or set()
        self.added: list[dict[str, Any]] = []

    async def unreceipted(
        self, since: datetime, until: Optional[datetime] = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        taken = {row["payment_id"] for row in self.added}
        return [p for p in self.payments if p["payment_id"] not in taken]

    async def known_uuids(self, uuids: Any) -> set[str]:
        return {u for u in uuids if u in self.known}

    async def add_issued(self, rows: Any) -> int:
        self.added.extend(rows)
        return len(rows)


def importer(payments: list[dict[str, Any]], incomes: list[CabinetIncome], **kw: Any) -> Any:
    dao, cabinet_ = ImportDao(payments, **kw), ImportCabinet(incomes)
    receipts, notifier = service(dao, cabinet_)  # type: ignore[arg-type]
    return receipts, dao, cabinet_, notifier


async def test_the_preview_writes_nothing() -> None:
    receipts, dao, cabinet_, _ = importer([payment(250, at(9))], [income(250, at(11))])
    plan = await receipts.import_plan(DAY)
    assert len(plan.pairs) == 1 and plan.inn and not dao.added and not cabinet_.registered


async def test_only_the_pairs_shown_are_tied_and_nothing_is_sent() -> None:
    p1, p2 = payment(250, at(9)), payment(500, at(9))
    i1, i2 = income(250, at(11)), income(500, at(11))
    receipts, dao, cabinet_, notifier = importer([p1, p2], [i1, i2])
    plan = await receipts.import_plan(DAY)
    shown = {pair.key for pair in plan.pairs if pair.income is i1}
    assert await receipts.apply_import(DAY, shown) == 1
    (row,) = dao.added
    assert row["payment_id"] == p1["payment_id"] and row["receipt_uuid"] == i1.receipt_uuid
    assert row["name"] == "Покупка подписки на 1 месяц" and row["inn"]
    assert not cabinet_.registered and not notifier.sent


async def test_a_receipt_already_tied_is_not_tied_again() -> None:
    i1 = income(250, at(11), "200a")
    receipts, _, _, _ = importer([payment(250, at(9))], [i1], known={"200a"})
    plan = await receipts.import_plan(DAY)
    assert not plan.pairs and len(plan.lone_payments) == 1
