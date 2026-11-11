"""A plan change of the "s" edition (docs/s/README.md), kept with the price.

It rides in the transaction's ``pricing`` JSON, so it needs no column and no
migration: a transaction written before it existed loads with ``None``.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Optional


class ChangeMode(StrEnum):
    # The end date stays; the difference of the daily prices is paid.
    KEEP_EXPIRY = "keep"
    # A new term from today; what is left of the current plan comes off.
    FROM_TODAY = "today"


@dataclass(kw_only=True)
class PlanChangeDto:
    mode: Optional[ChangeMode] = None
    # The new plan's own price for the term, after discounts, before `credit`.
    plan_amount: Optional[Decimal] = None
    # What is left of the current plan, taken off the price (FROM_TODAY).
    credit: Decimal = Decimal(0)
    # Devices to remove before the new, lower device limit is set.
    drop_hwids: list[str] = field(default_factory=list)
