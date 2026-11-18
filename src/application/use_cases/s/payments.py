"""A payment confirmed by hand (docs/s/README.md).

When the money arrived but the gateway's webhook did not (or came after the
bot had cancelled the order), an admin who checked the gateway's cabinet
confirms the order. It then goes the same way a webhook takes it: the
subscription, the customer's notice, the referral reward, the receipt and the
statistics. A payment is never confirmed twice: the transition to COMPLETED
happens once, whoever comes first, the admin or a late webhook.
"""

from enum import StrEnum, auto
from uuid import UUID

from loguru import logger

from src.application.common import Interactor, Notifier
from src.application.common.dao import TransactionDao, UserDao
from src.application.common.policy import Permission
from src.application.dto import MessagePayloadDto, TransactionDto, UserDto
from src.application.use_cases.gateways.commands.payment import ProcessPayment, ProcessPaymentDto
from src.core.enums import Role, TransactionStatus

# Statuses an order may be confirmed from: never paid as far as the bot knows.
CONFIRMABLE = frozenset(
    {TransactionStatus.PENDING, TransactionStatus.CANCELED, TransactionStatus.FAILED}
)


def can_confirm(transaction: TransactionDto) -> bool:
    return not transaction.is_test and transaction.status in CONFIRMABLE


class ConfirmOutcome(StrEnum):
    CONFIRMED = auto()
    ALREADY_PAID = auto()  # a webhook or another admin was first
    NOT_CONFIRMABLE = auto()  # a test, refunded, or gone
    FAILED = auto()  # the transition happened elsewhere, or nothing changed


class ConfirmPaymentByHand(Interactor[UUID, ConfirmOutcome]):
    # money: those who may change the bot's extra settings, OWNER and DEV
    required_permission = Permission.SETTINGS_EXTRA

    def __init__(
        self,
        transaction_dao: TransactionDao,
        user_dao: UserDao,
        process_payment: ProcessPayment,
        notifier: Notifier,
    ) -> None:
        self.transaction_dao = transaction_dao
        self.user_dao = user_dao
        self.process_payment = process_payment
        self.notifier = notifier

    async def _execute(self, actor: UserDto, data: UUID) -> ConfirmOutcome:
        transaction = await self.transaction_dao.get_by_payment_id(data)
        if transaction is None:
            return ConfirmOutcome.NOT_CONFIRMABLE
        if transaction.status == TransactionStatus.COMPLETED:
            return ConfirmOutcome.ALREADY_PAID
        if not can_confirm(transaction):
            return ConfirmOutcome.NOT_CONFIRMABLE

        logger.warning(
            f"{actor.log} Confirms payment '{data}' by hand "
            f"({transaction.pricing.final_amount} {transaction.currency}, "
            f"{transaction.gateway_type}, was {transaction.status})"
        )
        await self.process_payment.system(
            ProcessPaymentDto(
                payment_id=data,
                new_transaction_status=TransactionStatus.COMPLETED,
                gateway_type=transaction.gateway_type,
            )
        )

        after = await self.transaction_dao.get_by_payment_id(data)
        if after is None or after.status != TransactionStatus.COMPLETED:
            return ConfirmOutcome.FAILED

        buyer = await self.user_dao.get_by_id(transaction.user_id)
        try:
            await self.notifier.notify_admins(
                MessagePayloadDto(
                    i18n_key="event-s-payment.confirmed-by-hand",
                    i18n_kwargs={
                        "admin": actor.name,
                        "buyer": buyer.name if buyer else str(transaction.user_id),
                        "amount": str(transaction.pricing.final_amount),
                        "currency": transaction.currency.symbol,
                        "gateway": transaction.gateway_type,
                        "payment_id": str(data),
                    },
                    delete_after=None,
                ),
                roles=[Role.OWNER, Role.DEV],
            )
        except Exception:
            logger.exception(f"Admins not told about payment '{data}' confirmed by hand")
        return ConfirmOutcome.CONFIRMED
