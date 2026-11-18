"""A payment the bot never saw paid: a late webhook, or an admin by hand."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.application.dto import UserDto
from src.application.use_cases.gateways.commands.payment import ProcessPayment, ProcessPaymentDto
from src.application.use_cases.s.payments import ConfirmOutcome, ConfirmPaymentByHand
from src.core.enums import PaymentGatewayType, Role, TransactionStatus

YOOKASSA = PaymentGatewayType.YOOKASSA


def transaction(status: TransactionStatus, is_test: bool = False, **kw: Any) -> Any:
    return SimpleNamespace(
        payment_id=kw.get("payment_id", uuid4()),
        user_id=7,
        status=status,
        is_test=is_test,
        gateway_type=YOOKASSA,
        currency=SimpleNamespace(symbol="₽"),
        pricing=SimpleNamespace(final_amount=Decimal(300)),
    )


class Transactions:
    """Keeps one transaction and moves it the way the database does."""

    def __init__(self, tx: Any) -> None:
        self.tx = tx
        self.allowed: list[tuple[TransactionStatus, ...]] = []

    async def get_by_payment_id(self, payment_id: UUID) -> Any:
        return self.tx if self.tx is not None and self.tx.payment_id == payment_id else None

    async def transition_status(
        self, payment_id: UUID, new_status: TransactionStatus, allowed: Any
    ) -> Any:
        allowed = tuple(allowed)
        self.allowed.append(allowed)
        if self.tx.status not in allowed:
            return None
        self.tx.status = new_status
        return self.tx


class Uow:
    async def __aenter__(self) -> "Uow":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class Users:
    async def get_by_id(self, user_id: int) -> Any:
        return SimpleNamespace(id=user_id, name="Buyer", log="[7]", remna_name="rs_7")


class Recorder:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def notify_admins(self, payload: Any, roles: Any = None) -> None:
        self.sent.append((payload, roles))


class Processor(ProcessPayment):
    """The real transition; what follows a success is only counted."""

    def __init__(self, transactions: Transactions) -> None:
        self.uow = Uow()  # type: ignore[assignment]
        self.user_dao = Users()  # type: ignore[assignment]
        self.transaction_dao = transactions  # type: ignore[assignment]
        self.succeeded: list[Any] = []

    async def _handle_success(self, user: Any, transaction: Any) -> None:
        self.succeeded.append(transaction.payment_id)


ADMIN = UserDto(id=1, telegram_id=1, name="Owner", role=Role.OWNER)


def completed(tx: Any) -> ProcessPaymentDto:
    return ProcessPaymentDto(
        payment_id=tx.payment_id,
        new_transaction_status=TransactionStatus.COMPLETED,
        gateway_type=YOOKASSA,
    )


# -- the webhook ----------------------------------------------------------------


async def test_a_payment_confirmed_after_the_auto_cancel_still_counts() -> None:
    # the bot cancels an unpaid order after 30 minutes; the bank paid later
    tx = transaction(TransactionStatus.CANCELED)
    transactions = Transactions(tx)
    processor = Processor(transactions)
    await processor.system(completed(tx))
    assert tx.status == TransactionStatus.COMPLETED
    assert processor.succeeded == [tx.payment_id]


@pytest.mark.parametrize("status", [TransactionStatus.COMPLETED, TransactionStatus.REFUNDED])
async def test_a_paid_or_refunded_order_is_not_paid_again(status: TransactionStatus) -> None:
    tx = transaction(status)
    processor = Processor(Transactions(tx))
    await processor.system(completed(tx))
    assert tx.status == status and not processor.succeeded


async def test_two_confirmations_pay_once() -> None:
    tx = transaction(TransactionStatus.PENDING)
    processor = Processor(Transactions(tx))
    await processor.system(completed(tx))
    await processor.system(completed(tx))
    assert processor.succeeded == [tx.payment_id]


# -- by hand -------------------------------------------------------------------


def by_hand(tx: Any) -> tuple[ConfirmPaymentByHand, Processor, Recorder]:
    transactions = Transactions(tx)
    processor, notifier = Processor(transactions), Recorder()
    use_case = ConfirmPaymentByHand(transactions, Users(), processor, notifier)  # type: ignore[arg-type]
    return use_case, processor, notifier


@pytest.mark.parametrize(
    "status", [TransactionStatus.PENDING, TransactionStatus.CANCELED, TransactionStatus.FAILED]
)
async def test_an_admin_confirms_an_unpaid_order(status: TransactionStatus) -> None:
    tx = transaction(status)
    use_case, processor, notifier = by_hand(tx)
    assert await use_case(ADMIN, tx.payment_id) == ConfirmOutcome.CONFIRMED
    assert tx.status == TransactionStatus.COMPLETED and processor.succeeded == [tx.payment_id]
    ((payload, roles),) = notifier.sent
    assert payload.i18n_key == "event-s-payment.confirmed-by-hand"
    assert payload.i18n_kwargs["admin"] == "Owner" and payload.i18n_kwargs["amount"] == "300"
    assert roles == [Role.OWNER, Role.DEV]


async def test_an_order_already_paid_is_left_alone() -> None:
    tx = transaction(TransactionStatus.COMPLETED)
    use_case, processor, notifier = by_hand(tx)
    assert await use_case(ADMIN, tx.payment_id) == ConfirmOutcome.ALREADY_PAID
    assert not processor.succeeded and not notifier.sent


@pytest.mark.parametrize(
    "tx",
    [
        transaction(TransactionStatus.REFUNDED),
        transaction(TransactionStatus.PENDING, is_test=True),
    ],
)
async def test_a_refunded_or_test_order_is_refused(tx: Any) -> None:
    use_case, processor, notifier = by_hand(tx)
    assert await use_case(ADMIN, tx.payment_id) == ConfirmOutcome.NOT_CONFIRMABLE
    assert not processor.succeeded and not notifier.sent


async def test_an_unknown_order_is_refused() -> None:
    use_case, processor, _ = by_hand(transaction(TransactionStatus.PENDING))
    assert await use_case(ADMIN, uuid4()) == ConfirmOutcome.NOT_CONFIRMABLE
    assert not processor.succeeded


async def test_only_owner_and_dev_confirm_by_hand() -> None:
    from src.core.exceptions import PermissionDeniedError  # noqa: PLC0415

    tx = transaction(TransactionStatus.PENDING)
    use_case, processor, _ = by_hand(tx)
    admin = UserDto(id=2, telegram_id=2, name="Admin", role=Role.ADMIN)
    with pytest.raises(PermissionDeniedError):
        await use_case(admin, tx.payment_id)
    assert tx.status == TransactionStatus.PENDING and not processor.succeeded


@pytest.mark.parametrize(
    "key",
    [
        "event-s-payment.confirmed-by-hand",
        *(f"ntf-s-payment.{o.value.replace('_', '-')}" for o in ConfirmOutcome),
        "msg-s-payment.asked",
    ],
)
def test_the_texts_render(key: str) -> None:
    from tests.test_s_translations import i18n as translator  # noqa: PLC0415

    runner = translator.__wrapped__()  # type: ignore[attr-defined]
    args = {"admin": "A", "buyer": "B", "amount": "300", "currency": "₽"}
    text = runner.get(key, gateway="YOOKASSA", payment_id="x", **args)
    assert text and text != key and "{" not in text
