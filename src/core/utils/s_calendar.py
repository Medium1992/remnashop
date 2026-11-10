"""Calendar terms for plan durations (docs/s/README.md).

A plan stores its duration in days, and the bot already names 365 days a
year and 30 days a month. With calendar months on, the term follows that
name: a month ends on the same day of the next month, not 30 days later.
"""

from calendar import monthrange
from datetime import datetime, timedelta

DAYS_IN_YEAR = 365
DAYS_IN_MONTH = 30


def add_months(start: datetime, months: int) -> datetime:
    """``start`` moved ``months`` ahead; a missing day falls on the month's last."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def add_term(start: datetime, days: int) -> datetime:
    """The end of a ``days`` term started at ``start``, by the calendar.

    Read the same way the duration is named: whole years first, then whole
    months, anything else in days.
    """
    if days > 0 and days % DAYS_IN_YEAR == 0:
        return add_months(start, 12 * (days // DAYS_IN_YEAR))
    if days > 0 and days % DAYS_IN_MONTH == 0:
        return add_months(start, days // DAYS_IN_MONTH)
    return start + timedelta(days=days)
