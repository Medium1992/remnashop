"""Receipts made by hand in "Мой налог" before the bot made them, tied to the
bot's payments (docs/s/README.md).

The cabinet knows no payment ids: a receipt has an amount and the time it was
made, and a receipt made by hand is made after the payment, often much later
and in batches. So they are paired by amount and order: each receipt, oldest
first, takes the oldest payment of the same amount still free that was made
no later than the receipt. Nothing is registered in the cabinet and nothing is
sent: the payment only gets the receipt it already had.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

from .nalog import CabinetIncome

# A receipt made a little before the payment shows here (clocks differ).
TOLERANCE: Final[timedelta] = timedelta(minutes=5)


@dataclass(frozen=True)
class ImportPair:
    payment: dict[str, Any]
    income: CabinetIncome

    @property
    def key(self) -> str:
        return f"{self.payment['payment_id']}:{self.income.receipt_uuid}"


@dataclass
class ImportPlan:
    pairs: list[ImportPair] = field(default_factory=list)
    lone_payments: list[dict[str, Any]] = field(default_factory=list)
    lone_incomes: list[CabinetIncome] = field(default_factory=list)
    inn: str = ""

    @property
    def paired_amount(self) -> Decimal:
        return sum((pair.income.amount for pair in self.pairs), Decimal(0))

    @property
    def lone_payments_amount(self) -> Decimal:
        return sum((Decimal(p["amount"]) for p in self.lone_payments), Decimal(0))

    @property
    def lone_incomes_amount(self) -> Decimal:
        return sum((income.amount for income in self.lone_incomes), Decimal(0))


def match(payments: list[dict[str, Any]], incomes: list[CabinetIncome]) -> ImportPlan:
    """Pairs by amount and order; what found no pair is left on its side."""
    free: dict[Decimal, list[dict[str, Any]]] = {}
    for payment in sorted(payments, key=lambda p: p["paid_at"]):
        free.setdefault(Decimal(payment["amount"]), []).append(payment)

    plan = ImportPlan()
    for income in sorted(incomes, key=lambda i: i.operation_time):
        queue = free.get(income.amount, [])
        chosen = next((p for p in queue if p["paid_at"] <= income.operation_time + TOLERANCE), None)
        if chosen is None:
            plan.lone_incomes.append(income)
            continue
        queue.remove(chosen)
        plan.pairs.append(ImportPair(payment=chosen, income=income))

    plan.lone_payments = sorted(
        (p for queue in free.values() for p in queue), key=lambda p: p["paid_at"]
    )
    return plan


def usable(incomes: list[CabinetIncome], known: set[str]) -> list[CabinetIncome]:
    """Receipts that may stand for a payment: to a private person, not taken
    back, and not tied to a payment already."""
    return [
        income
        for income in incomes
        if income.receipt_uuid
        and income.from_individual
        and not income.canceled
        and income.receipt_uuid not in known
    ]
