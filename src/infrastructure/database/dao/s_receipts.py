from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common import Cryptographer
from src.application.dto.s_receipts import NalogAccountDto, NalogLogin, ReceiptDto, ReceiptStatus
from src.core.enums import Currency, PurchaseType, TransactionStatus
from src.infrastructure.database.models import Transaction
from src.infrastructure.database.models.s import SNalogAccount, SReceipt

ACCOUNT_ID = 1
# Columns stored encrypted: what opens the cabinet, or tells who owns it.
SECRET_FIELDS = ("phone", "password", "refresh_token", "token", "proxy")


class NalogAccountDao:
    """The one row of ``s_nalog_account``; secrets go in and out encrypted."""

    def __init__(self, session: AsyncSession, cryptographer: Cryptographer) -> None:
        self.session = session
        self.cryptographer = cryptographer

    async def get(self, lock: bool = False) -> NalogAccountDto:
        """With ``lock``, the row stays locked until the transaction ends, so
        two processes never refresh the login at once."""
        # Fresh from the database every time: ``save`` writes past the ORM, and
        # another process may have written since.
        stmt = (
            select(SNalogAccount)
            .where(SNalogAccount.id == ACCOUNT_ID)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        row = await self.session.scalar(stmt)
        if row is None:
            return NalogAccountDto()
        return NalogAccountDto(
            inn=row.inn,
            method=NalogLogin(row.method) if row.method else None,
            phone=self._open(row.phone),
            password=self._open(row.password),
            refresh_token=self._open(row.refresh_token),
            token=self._open(row.token),
            token_expires_at=row.token_expires_at,
            device_id=row.device_id or "",
            proxy=self._open(row.proxy),
            lost_at=row.lost_at,
            lost_reason=row.lost_reason,
        )

    async def save(self, account: NalogAccountDto) -> None:
        values = {
            "inn": account.inn,
            "method": account.method.value if account.method else None,
            "token_expires_at": account.token_expires_at,
            "device_id": account.device_id,
            "lost_at": account.lost_at,
            "lost_reason": account.lost_reason,
        }
        for name in SECRET_FIELDS:
            values[name] = self._close(getattr(account, name))
        stmt = insert(SNalogAccount).values(id=ACCOUNT_ID, **values)
        stmt = stmt.on_conflict_do_update(index_elements=[SNalogAccount.id], set_=values)
        await self.session.execute(stmt)

    def _close(self, value: Optional[str]) -> Optional[str]:
        return self.cryptographer.encrypt(value) if value else None

    def _open(self, value: Optional[str]) -> Optional[str]:
        return self.cryptographer.decrypt(value) if value else None


def payment_amount(pricing: dict[str, Any]) -> Decimal:
    return Decimal(str(pricing.get("final_amount") or 0))


def _dto(row: SReceipt) -> ReceiptDto:
    return ReceiptDto(
        id=row.id,
        payment_id=row.payment_id,
        user_id=row.user_id,
        amount=row.amount,
        paid_at=row.paid_at,
        purchase_type=PurchaseType(row.purchase_type),
        duration=row.duration,
        status=ReceiptStatus(row.status),
        name=row.name,
        receipt_uuid=row.receipt_uuid,
        inn=row.inn,
        client_inn=row.client_inn,
        client_name=row.client_name,
        attempts=row.attempts,
        error=row.error,
        delivery_error=row.delivery_error,
        delivered_at=row.delivered_at,
        canceled_at=row.canceled_at,
    )


# A receipt the buyer can be given: in the cabinet and not taken back.
ISSUED = (ReceiptStatus.REGISTERED.value, ReceiptStatus.DELIVERED.value)
# What an admin has to see to: no receipt, or one the buyer never got.
UNISSUED = (
    ReceiptStatus.FAILED.value,
    ReceiptStatus.UNKNOWN.value,
)


@dataclass
class ReceiptSummary:
    issued: int = 0
    waiting: int = 0
    unissued: int = 0
    unissued_amount: Decimal = Decimal(0)
    undelivered: int = 0
    undelivered_amount: Decimal = Decimal(0)


class ReceiptDao:
    """``s_receipts``: one row per paid payment, so a payment never gets two."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _select(self) -> Select[tuple[SReceipt]]:
        return select(SReceipt).execution_options(populate_existing=True)

    async def enqueue_paid(self, since: datetime, until: Optional[datetime] = None) -> int:
        """Rows for real-money (rouble) payments completed since ``since`` that
        have none yet; how many were added."""
        known = select(SReceipt.payment_id)
        stmt = (
            select(Transaction)
            .where(
                Transaction.status == TransactionStatus.COMPLETED,
                Transaction.is_test.is_(False),
                Transaction.currency == Currency.RUB,
                Transaction.updated_at >= since,
                Transaction.payment_id.not_in(known),
            )
            .order_by(Transaction.updated_at)
            .limit(500)
        )
        if until is not None:
            stmt = stmt.where(Transaction.updated_at < until)
        rows = []
        for transaction in await self.session.scalars(stmt):
            amount = payment_amount(transaction.pricing)
            if amount <= 0:
                continue
            rows.append(
                {
                    "payment_id": transaction.payment_id,
                    "user_id": transaction.user_id,
                    "amount": amount,
                    "paid_at": transaction.updated_at,
                    "purchase_type": PurchaseType(transaction.purchase_type).value,
                    "duration": int(transaction.plan_snapshot.get("duration") or 0),
                    "status": ReceiptStatus.PENDING.value,
                }
            )
        if not rows:
            return 0
        result = await self.session.execute(
            insert(SReceipt)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[SReceipt.payment_id])
            .returning(SReceipt.id)
        )
        return len(result.all())

    async def due(self, now: datetime, limit: int = 20) -> list[ReceiptDto]:
        stmt = (
            self._select()
            .where(
                SReceipt.status == ReceiptStatus.PENDING.value,
                or_(SReceipt.next_try_at.is_(None), SReceipt.next_try_at <= now),
            )
            .order_by(SReceipt.paid_at)
            .limit(limit)
        )
        return [_dto(row) for row in await self.session.scalars(stmt)]

    async def undelivered_fresh(self, limit: int = 20) -> list[ReceiptDto]:
        """In the cabinet, never tried on the buyer (the bot stopped in between)."""
        stmt = (
            self._select()
            .where(
                SReceipt.status == ReceiptStatus.REGISTERED.value,
                SReceipt.delivery_error.is_(None),
            )
            .order_by(SReceipt.paid_at)
            .limit(limit)
        )
        return [_dto(row) for row in await self.session.scalars(stmt)]

    async def refunded(self, limit: int = 20) -> list[ReceiptDto]:
        """Issued receipts of payments since refunded, not yet failed to cancel."""
        stmt = (
            self._select()
            .join(Transaction, Transaction.payment_id == SReceipt.payment_id)
            .where(
                SReceipt.status.in_(ISSUED),
                Transaction.status == TransactionStatus.REFUNDED,
                or_(SReceipt.error.is_(None), SReceipt.error.not_like("cancel:%")),
            )
            .limit(limit)
        )
        return [_dto(row) for row in await self.session.scalars(stmt)]

    async def claim(self, receipt_id: int, name: str) -> bool:
        """PENDING -> SENDING, once: only the claimer sends it to the cabinet."""
        result = await self.session.execute(
            update(SReceipt)
            .where(SReceipt.id == receipt_id, SReceipt.status == ReceiptStatus.PENDING.value)
            .values(
                status=ReceiptStatus.SENDING.value,
                name=name,
                attempts=SReceipt.attempts + 1,
            )
            .returning(SReceipt.id)
        )
        return result.first() is not None

    async def mark(
        self,
        receipt_id: int,
        status: ReceiptStatus,
        *,
        expect: Optional[Sequence[ReceiptStatus]] = None,
        **values: Any,
    ) -> bool:
        stmt = update(SReceipt).where(SReceipt.id == receipt_id)
        if expect is not None:
            stmt = stmt.where(SReceipt.status.in_([s.value for s in expect]))
        result = await self.session.execute(
            stmt.values(status=status.value, **values).returning(SReceipt.id)
        )
        return result.first() is not None

    async def note(self, receipt_id: int, **values: Any) -> None:
        await self.session.execute(
            update(SReceipt).where(SReceipt.id == receipt_id).values(**values)
        )

    async def get(self, receipt_id: int) -> Optional[ReceiptDto]:
        row = await self.session.scalar(self._select().where(SReceipt.id == receipt_id))
        return _dto(row) if row else None

    async def by_payment(self, payment_id: UUID) -> Optional[ReceiptDto]:
        row = await self.session.scalar(self._select().where(SReceipt.payment_id == payment_id))
        return _dto(row) if row else None

    async def of_user(
        self, user_id: int, issued_only: bool = True, limit: int = 50
    ) -> list[ReceiptDto]:
        stmt = self._select().where(SReceipt.user_id == user_id)
        if issued_only:
            stmt = stmt.where(SReceipt.status.in_(ISSUED))
        stmt = stmt.order_by(SReceipt.paid_at.desc()).limit(limit)
        return [_dto(row) for row in await self.session.scalars(stmt)]

    async def summary(self) -> "ReceiptSummary":
        stmt = select(
            SReceipt.status,
            SReceipt.delivery_error.is_not(None),
            func.count(SReceipt.id),
            func.coalesce(func.sum(SReceipt.amount), 0),
        ).group_by(SReceipt.status, SReceipt.delivery_error.is_not(None))
        summary = ReceiptSummary()
        for status, undelivered, count, amount in (await self.session.execute(stmt)).all():
            if status in UNISSUED:
                summary.unissued += count
                summary.unissued_amount += Decimal(amount)
            elif status == ReceiptStatus.REGISTERED.value and undelivered:
                summary.undelivered += count
                summary.undelivered_amount += Decimal(amount)
            elif status in (ReceiptStatus.PENDING.value, ReceiptStatus.SENDING.value):
                summary.waiting += count
            if status in ISSUED:
                summary.issued += count
        return summary

    async def count_of_user(self, user_id: int) -> int:
        stmt = select(func.count(SReceipt.id)).where(
            SReceipt.user_id == user_id, SReceipt.status.in_(ISSUED)
        )
        return int(await self.session.scalar(stmt) or 0)

    async def unissued(self, undelivered: bool, limit: int = 200) -> list[ReceiptDto]:
        """For the admin: no receipt (failed, unknown, stuck sending), or in the
        cabinet but never given to the buyer."""
        stmt = self._select()
        if undelivered:
            stmt = stmt.where(
                SReceipt.status == ReceiptStatus.REGISTERED.value,
                SReceipt.delivery_error.is_not(None),
            )
        else:
            stmt = stmt.where(SReceipt.status.in_(UNISSUED))
        stmt = stmt.order_by(SReceipt.paid_at).limit(limit)
        return [_dto(row) for row in await self.session.scalars(stmt)]

    async def stuck_sending(self, before: datetime) -> int:
        """SENDING for too long: the bot stopped mid-request. Nobody knows if the
        income went in, so these go to the admin, never back in the queue."""
        result = await self.session.execute(
            update(SReceipt)
            .where(
                SReceipt.status == ReceiptStatus.SENDING.value,
                SReceipt.updated_at < before,
            )
            .values(
                status=ReceiptStatus.UNKNOWN.value,
                error="stopped while sending",
            )
            .returning(SReceipt.id)
        )
        return len(result.all())
