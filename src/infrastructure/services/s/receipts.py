"""Receipts of the self-employed in "Мой налог", issued after each payment
(docs/s/README.md).

Nothing of the payment waits for a receipt. A completed rouble payment gets a
row in ``s_receipts`` -- right after the purchase, from the purchase event, and
on a sweep every minute for whatever the event missed. The row is then sent to
the cabinet and the receipt to the buyer.

Two receipts for one payment are the failure to avoid above all, so:

- one row per payment (``payment_id`` is unique);
- a row is claimed (PENDING -> SENDING) before it goes out, so two runs never
  send the same one;
- only a failure that is known to have left nothing in the cabinet puts it
  back in the queue. Sent with no clear answer, or stopped mid-request, it
  becomes UNKNOWN and waits for an admin, who looks in the cabinet.

A refused request (a limit, a date the cabinet won't take) is FAILED at once;
the cabinet being away is retried for a while first. Either way admins hear of
it, and the receipts window lists it until it is dealt with.
"""

import html
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile
from loguru import logger
from redis.asyncio import Redis

from src.application.common import Notifier, TranslatorHub
from src.application.common.dao import SettingsDao, UserDao
from src.application.common.uow import UnitOfWork
from src.application.dto import MessagePayloadDto
from src.application.dto.s_receipts import ReceiptDto, ReceiptStatus
from src.application.events.system import UserPurchaseEvent
from src.core.enums import Locale, PurchaseType, Role
from src.core.utils.time import datetime_now
from src.infrastructure.database.dao.s_receipts import ReceiptDao, ReceiptSummary
from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import ReceiptsRunKey
from src.infrastructure.services.event_bus import on_event

from .nalog import (
    CANCEL_REFUND,
    CANCEL_WRONG,
    MOSCOW,
    NalogAuthError,
    NalogClient,
    NalogError,
    NalogUnknownError,
    NalogUnreachableError,
    ReceiptFile,
    receipt_url,
)
from .nalog_auth import NalogAuth, NalogLoginLostError, NalogNotLoggedInError

# Minutes to wait after each failed try while the cabinet is away.
BACKOFF_MINUTES: Final[tuple[int, ...]] = (1, 5, 15, 30, 60, 120, 240, 480)
MAX_ATTEMPTS: Final[int] = 12
PER_RUN: Final[int] = 10
RUN_LOCK_SECONDS: Final[int] = 900
# Longer than any one request can take: a row SENDING this long was left mid-way.
STUCK_AFTER: Final[timedelta] = timedelta(minutes=20)
FILE_TYPES: Final[frozenset[str]] = frozenset({"png", "jpg", "pdf"})


def receipt_term(days: int) -> tuple[str, int]:
    """A plan's duration as the receipt names it."""
    if days <= 0:
        return "none", 0
    if days % 365 == 0:
        return "years", days // 365
    if days % 30 == 0:
        return "months", days // 30
    return "days", days


def backoff(attempts: int) -> timedelta:
    index = min(max(attempts, 1), len(BACKOFF_MINUTES)) - 1
    return timedelta(minutes=BACKOFF_MINUTES[index])


def short(error: BaseException) -> str:
    return str(error)[:500] or type(error).__name__


@dataclass
class RunReport:
    added: int = 0
    registered: int = 0
    delivered: int = 0
    # Receipts that need an admin now: (receipt, what happened).
    problems: list[tuple[ReceiptDto, str]] = field(default_factory=list)


class Receipts:
    def __init__(
        self,
        uow: UnitOfWork,
        receipt_dao: ReceiptDao,
        settings_dao: SettingsDao,
        user_dao: UserDao,
        auth: NalogAuth,
        notifier: Notifier,
        translator_hub: TranslatorHub,
        bot: Bot,
        redis: Redis,
    ) -> None:
        self.uow = uow
        self.receipt_dao = receipt_dao
        self.settings_dao = settings_dao
        self.user_dao = user_dao
        self.auth = auth
        self.notifier = notifier
        self.translator_hub = translator_hub
        self.bot = bot
        self.redis = redis

    async def enabled(self) -> bool:
        return (await self.settings_dao.get()).extra.s.receipts.enabled

    @on_event(UserPurchaseEvent)
    async def on_purchase(self, event: UserPurchaseEvent) -> None:
        # Right after the purchase; the sweep catches it if this fails.
        try:
            await self.run()
        except Exception:
            logger.exception("Receipts run after a purchase failed")

    async def run(self) -> Optional[RunReport]:
        settings = (await self.settings_dao.get()).extra.s.receipts
        if not settings.enabled or not settings.since:
            return None

        key = serialize_storage_key(ReceiptsRunKey())
        owner = secrets.token_hex(8)
        if not await self.redis.set(key, owner, nx=True, ex=RUN_LOCK_SECONDS):
            logger.debug("Receipts run skipped: another one is going")
            return None
        try:
            return await self._run(datetime.fromisoformat(settings.since))
        finally:
            if await self.redis.get(key) in (owner, owner.encode()):
                await self.redis.delete(key)

    async def _run(self, since: datetime) -> RunReport:
        report = RunReport()
        async with self.uow:
            report.added = await self.receipt_dao.enqueue_paid(since)
            stuck = await self.receipt_dao.stuck_sending(datetime_now() - STUCK_AFTER)
            await self.uow.commit()
        if report.added:
            logger.info(f"Receipts: {report.added} paid payments queued")
        if stuck:
            logger.error(f"Receipts: {stuck} left mid-request, marked unknown")

        async with self.uow:
            due = await self.receipt_dao.due(datetime_now(), PER_RUN)
            fresh = await self.receipt_dao.undelivered_fresh(PER_RUN)
            refunded = await self.receipt_dao.refunded(PER_RUN)
        if not (due or fresh or refunded):
            return report

        await self._work(due, fresh, refunded, report)
        if report.problems:
            await self._tell_problems(report.problems)
        return report

    async def _work(
        self,
        due: list[ReceiptDto],
        fresh: list[ReceiptDto],
        refunded: list[ReceiptDto],
        report: RunReport,
    ) -> None:
        try:
            client, token, inn = await self.auth.session()
        except NalogLoginLostError as e:
            await self._tell_login_lost(str(e))
            return
        except (NalogNotLoggedInError, NalogUnreachableError) as e:
            logger.warning(f"Receipts wait: {e}")
            return

        for receipt in due:
            registered = await self._register(receipt, client, token, inn, report)
            if registered is not None:
                report.registered += 1
                report.delivered += await self.deliver(registered, client, token)
        for receipt in fresh:
            report.delivered += await self.deliver(receipt, client, token)
        for receipt in refunded:
            await self._cancel_refunded(receipt, client, token)

    # -- the cabinet --------------------------------------------------------

    def receipt_name(self, receipt: ReceiptDto) -> str:
        # The receipt is a tax paper: Russian whoever bought.
        i18n = self.translator_hub.get_translator_by_locale(Locale.RU)
        unit, value = receipt_term(receipt.duration)
        return i18n.get(
            "s-receipt-name",
            renew=int(receipt.purchase_type == PurchaseType.RENEW),
            unit=unit,
            value=value,
        )

    async def _register(
        self,
        receipt: ReceiptDto,
        client: NalogClient,
        token: str,
        inn: str,
        report: RunReport,
    ) -> Optional[ReceiptDto]:
        name = self.receipt_name(receipt)
        async with self.uow:
            claimed = await self.receipt_dao.claim(receipt.id, name)
            await self.uow.commit()
        if not claimed:
            return None
        attempts = receipt.attempts + 1

        try:
            receipt_uuid = await client.create_income(token, name, receipt.amount, receipt.paid_at)
        except NalogAuthError as e:
            # Refused before anything was done; the token goes, the row waits.
            await self._back_to_queue(receipt, attempts - 1, short(e))
            await self.auth.forget_token()
            return None
        except NalogUnreachableError as e:
            if attempts >= MAX_ATTEMPTS:
                await self._mark(receipt, ReceiptStatus.FAILED, error=short(e))
                report.problems.append((receipt, short(e)))
            else:
                await self._back_to_queue(receipt, attempts, short(e))
            return None
        except NalogUnknownError as e:
            await self._mark(receipt, ReceiptStatus.UNKNOWN, error=short(e))
            report.problems.append((receipt, short(e)))
            return None
        except NalogError as e:
            await self._mark(receipt, ReceiptStatus.FAILED, error=short(e))
            report.problems.append((receipt, short(e)))
            return None
        except Exception as e:
            logger.exception(f"Receipt '{receipt.id}' failed while sending")
            await self._mark(receipt, ReceiptStatus.UNKNOWN, error=short(e))
            report.problems.append((receipt, short(e)))
            return None

        async with self.uow:
            await self.receipt_dao.mark(
                receipt.id,
                ReceiptStatus.REGISTERED,
                receipt_uuid=receipt_uuid,
                inn=inn,
                error=None,
                next_try_at=None,
            )
            await self.uow.commit()
        logger.info(f"Receipt '{receipt_uuid}' registered for payment '{receipt.payment_id}'")
        return replace(
            receipt,
            status=ReceiptStatus.REGISTERED,
            name=name,
            receipt_uuid=receipt_uuid,
            inn=inn,
        )

    async def _back_to_queue(self, receipt: ReceiptDto, attempts: int, error: str) -> None:
        async with self.uow:
            await self.receipt_dao.mark(
                receipt.id,
                ReceiptStatus.PENDING,
                attempts=attempts,
                error=error,
                next_try_at=datetime_now() + backoff(attempts),
            )
            await self.uow.commit()

    async def _mark(self, receipt: ReceiptDto, status: ReceiptStatus, **values: Any) -> None:
        async with self.uow:
            await self.receipt_dao.mark(receipt.id, status, **values)
            await self.uow.commit()
        logger.error(f"Receipt for payment '{receipt.payment_id}' is {status}: {values}")

    async def _cancel_refunded(self, receipt: ReceiptDto, client: NalogClient, token: str) -> None:
        try:
            await client.cancel_income(token, receipt.receipt_uuid or "", CANCEL_REFUND)
        except (NalogUnreachableError, NalogAuthError) as e:
            logger.warning(f"Receipt '{receipt.receipt_uuid}' not canceled yet: {e}")
            return
        except Exception as e:
            async with self.uow:
                await self.receipt_dao.note(receipt.id, error=f"cancel: {short(e)}")
                await self.uow.commit()
            logger.error(f"Receipt '{receipt.receipt_uuid}' of a refund not canceled: {e}")
            return
        async with self.uow:
            await self.receipt_dao.mark(
                receipt.id, ReceiptStatus.CANCELED, canceled_at=datetime_now(), error=None
            )
            await self.uow.commit()
        logger.info(f"Receipt '{receipt.receipt_uuid}' canceled: the payment was refunded")

    # -- the buyer ----------------------------------------------------------

    async def fetch_file(self, receipt: ReceiptDto) -> Optional[ReceiptFile]:
        """The receipt's picture from the tax service, kept in memory only."""
        if not receipt.receipt_uuid or not receipt.inn:
            return None
        try:
            client, token, _ = await self.auth.session()
            file = await client.receipt_file(receipt.inn, receipt.receipt_uuid, token)
        except Exception as e:
            logger.warning(f"Receipt '{receipt.receipt_uuid}' file not fetched: {e}")
            return None
        return file if file.extension in FILE_TYPES else None

    async def deliver(
        self,
        receipt: ReceiptDto,
        client: Optional[NalogClient] = None,
        token: Optional[str] = None,
    ) -> int:
        """Sends the receipt to the buyer: 1 if it reached them."""
        if not receipt.receipt_uuid or not receipt.inn:
            return 0
        user = await self.user_dao.get_by_id(receipt.user_id) if receipt.user_id else None
        if user is None or user.telegram_id is None:
            await self._delivery_failed(receipt, "no telegram")
            return 0

        file = None
        if client is not None:
            try:
                file = await client.receipt_file(receipt.inn, receipt.receipt_uuid, token)
            except Exception as e:
                logger.warning(f"Receipt '{receipt.receipt_uuid}' file not fetched: {e}")
            if file is not None and file.extension not in FILE_TYPES:
                file = None
        else:
            file = await self.fetch_file(receipt)

        i18n = self.translator_hub.get_translator_by_locale(user.language)
        text = i18n.get(
            "msg-s-receipt.sent",
            name=html.escape(receipt.name),
            amount=str(receipt.amount),
            url=receipt_url(receipt.inn, receipt.receipt_uuid),
        )
        try:
            if file is not None:
                await self.bot.send_document(
                    user.telegram_id,
                    BufferedInputFile(file.content, filename=file_name(receipt, file)),
                    caption=text,
                )
            else:
                await self.bot.send_message(user.telegram_id, text)
        except (TelegramNetworkError, TelegramRetryAfter) as e:
            # Telegram away: the next run tries again.
            logger.warning(f"Receipt '{receipt.receipt_uuid}' not sent yet: {e}")
            return 0
        except TelegramAPIError as e:
            await self._delivery_failed(receipt, short(e))
            return 0

        async with self.uow:
            await self.receipt_dao.mark(
                receipt.id,
                ReceiptStatus.DELIVERED,
                expect=(ReceiptStatus.REGISTERED, ReceiptStatus.DELIVERED),
                delivered_at=datetime_now(),
                delivery_error=None,
            )
            await self.uow.commit()
        return 1

    async def _delivery_failed(self, receipt: ReceiptDto, error: str) -> None:
        async with self.uow:
            await self.receipt_dao.note(receipt.id, delivery_error=error)
            await self.uow.commit()
        logger.warning(f"Receipt '{receipt.receipt_uuid}' not delivered: {error}")

    # -- what an admin does -------------------------------------------------

    async def summary(self) -> ReceiptSummary:
        async with self.uow:
            return await self.receipt_dao.summary()

    async def get(self, receipt_id: int) -> Optional[ReceiptDto]:
        async with self.uow:
            return await self.receipt_dao.get(receipt_id)

    async def unissued(self, undelivered: bool) -> list[ReceiptDto]:
        async with self.uow:
            return await self.receipt_dao.unissued(undelivered)

    async def of_user(self, user_id: int, issued_only: bool = True) -> list[ReceiptDto]:
        async with self.uow:
            return await self.receipt_dao.of_user(user_id, issued_only=issued_only)

    async def retry(self, receipt_id: int) -> bool:
        """Back in the queue: the admin made sure the cabinet has no receipt."""
        async with self.uow:
            done = await self.receipt_dao.mark(
                receipt_id,
                ReceiptStatus.PENDING,
                expect=(ReceiptStatus.FAILED, ReceiptStatus.UNKNOWN),
                attempts=0,
                error=None,
                next_try_at=None,
            )
            await self.uow.commit()
        if done:
            await self.run()
        return done

    async def set_manual(self, receipt_id: int, raw: str) -> Optional[ReceiptDto]:
        """The receipt the admin made in the cabinet by hand, then to the buyer."""
        receipt_uuid = parse_receipt_uuid(raw)
        if receipt_uuid is None:
            raise ValueError("Not a receipt number")
        inn = (await self.auth.account()).inn
        async with self.uow:
            done = await self.receipt_dao.mark(
                receipt_id,
                ReceiptStatus.REGISTERED,
                expect=(ReceiptStatus.FAILED, ReceiptStatus.UNKNOWN),
                receipt_uuid=receipt_uuid,
                inn=inn,
                error=None,
                delivery_error=None,
            )
            await self.uow.commit()
        if not done:
            return None
        receipt = await self.get(receipt_id)
        if receipt is not None:
            await self.deliver(receipt)
        return await self.get(receipt_id)

    async def redeliver(self, receipt_id: int) -> bool:
        receipt = await self.get(receipt_id)
        if receipt is None or receipt.status not in (
            ReceiptStatus.REGISTERED,
            ReceiptStatus.DELIVERED,
        ):
            return False
        return bool(await self.deliver(receipt))

    async def reissue_to_organisation(self, receipt_id: int, inn: str, name: str) -> None:
        """Takes the receipt back as made by mistake and makes it out again, to
        the organisation that paid. Raises if the old one could not be taken back:
        then nothing changed."""
        receipt = await self.get(receipt_id)
        if receipt is None or receipt.status not in (
            ReceiptStatus.REGISTERED,
            ReceiptStatus.DELIVERED,
        ):
            raise ValueError("Only an issued receipt can be made out again")
        client, token, _ = await self.auth.session()
        await client.cancel_income(token, receipt.receipt_uuid or "", CANCEL_WRONG)
        logger.info(f"Receipt '{receipt.receipt_uuid}' canceled to make it out to INN {inn}")
        async with self.uow:
            await self.receipt_dao.mark(
                receipt_id,
                ReceiptStatus.PENDING,
                receipt_uuid=None,
                client_inn=inn,
                client_name=name,
                attempts=0,
                error=None,
                delivery_error=None,
                next_try_at=None,
            )
            await self.uow.commit()
        await self.run()

    async def add_since(self, since: datetime) -> int:
        """Payments before the switch-on, from ``since``, queued for receipts."""
        settings = (await self.settings_dao.get()).extra.s.receipts
        until = datetime.fromisoformat(settings.since) if settings.since else datetime_now()
        async with self.uow:
            added = await self.receipt_dao.enqueue_paid(since, until)
            await self.uow.commit()
        logger.info(f"Receipts: {added} earlier payments queued, from {since}")
        if added:
            await self.run()
        return added

    # -- admins -------------------------------------------------------------

    async def _tell_problems(self, problems: list[tuple[ReceiptDto, str]]) -> None:
        total = sum((receipt.amount for receipt, _ in problems), Decimal(0))
        lines = "\n".join(
            f"• {receipt.amount} ₽, {receipt.paid_at.astimezone(MOSCOW):%d.%m.%Y %H:%M}: "
            f"{html.escape(reason[:120])}"
            for receipt, reason in problems[:10]
        )
        await self._tell(
            "event-s-receipts.problems",
            count=len(problems),
            amount=str(total),
            lines=lines,
        )

    async def _tell_login_lost(self, reason: str) -> None:
        await self._tell("event-s-receipts.login-lost", reason=html.escape(reason[:300]))

    async def _tell(self, key: str, **kwargs: object) -> None:
        try:
            await self.notifier.notify_admins(
                MessagePayloadDto(i18n_key=key, i18n_kwargs=dict(kwargs), delete_after=None),
                # Those who can open the S dashboard to deal with it.
                roles=[Role.OWNER, Role.DEV],
            )
        except Exception:
            logger.exception(f"Admins not told '{key}'")


def parse_receipt_uuid(raw: str) -> Optional[str]:
    """A receipt number as typed, or taken from its link."""
    text = raw.strip()
    match = re.search(r"/receipt/\d+/([0-9A-Za-z]+)", text)
    value = match.group(1) if match else text
    return value.lower() if re.fullmatch(r"[0-9A-Za-z]{6,64}", value) else None


def file_name(receipt: ReceiptDto, file: ReceiptFile) -> str:
    return f"receipt_{receipt.paid_at.astimezone(MOSCOW):%Y-%m-%d}_{receipt.id}.{file.extension}"
