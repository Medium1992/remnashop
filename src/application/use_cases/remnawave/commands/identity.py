"""Re-key subscriptions that still hold a real panel uuid.

RemnaShop used to store the panel's ``users.uuid`` in ``user_remna_id``. The
panel dropped that column in 3.0.0, so those rows point at an identifier that no
longer exists and every call made for them would fail. This walks them once and
rewrites each to the encoded numeric id, which is stable across the whole
supported range.

It runs at startup, before the bot serves anything, and is safe to run again:
a row that already holds an encoded id is not selected, and a row that cannot be
resolved is left exactly as it was rather than being guessed at. Nothing is
deleted and no column changes -- only the value inside an existing one.

How a row is resolved depends on which panel is answering:

* on 2.x the stored uuid still works, so the id comes straight back;
* on 3.x it does not exist anywhere, so the shortUuid embedded in the stored
  subscription url is used -- it survives the panel's upgrade untouched -- and
  the username is the last resort.
"""

from dataclasses import dataclass

from loguru import logger

from src.application.common import Remnawave
from src.application.common.dao import SubscriptionDao
from src.application.common.uow import UnitOfWork
from src.core.utils.remna_id import encode_remna_id

# Each pass takes a bounded slice so a large install does not build one enormous
# transaction; the loop simply comes back for the next slice.
_BATCH_SIZE = 200

# A ceiling on one run (100k subscriptions); anything past it is picked up on
# the next start, since the cursor always begins from the lowest legacy id.
_MAX_BATCHES = 500


@dataclass(frozen=True)
class BackfillReport:
    resolved: int = 0
    unresolved: int = 0

    @property
    def total(self) -> int:
        return self.resolved + self.unresolved


class BackfillRemnaIdentity:
    def __init__(
        self,
        uow: UnitOfWork,
        subscription_dao: SubscriptionDao,
        remnawave: Remnawave,
    ) -> None:
        self.uow = uow
        self.subscription_dao = subscription_dao
        self.remnawave = remnawave

    async def run(self) -> BackfillReport:
        resolved = 0
        unresolved = 0
        after_id = 0

        for _ in range(_MAX_BATCHES):
            batch = await self.subscription_dao.get_legacy_identities(
                _BATCH_SIZE,
                after_id=after_id,
            )
            if not batch:
                break

            # Walk forward by id. Rows that cannot be resolved stay legacy, so
            # re-reading "the first N legacy rows" would return them forever and
            # never reach anything behind them.
            after_id = batch[-1].subscription_id

            async with self.uow:
                for row in batch:
                    panel_id = await self.remnawave.resolve_legacy_identity(
                        row.user_remna_id,
                        subscription_url=row.url,
                        username=row.username,
                    )

                    if panel_id is None:
                        unresolved += 1
                        logger.warning(
                            f"Subscription '{row.subscription_id}' still holds the legacy "
                            f"identity '{row.user_remna_id}' and could not be matched to a "
                            f"panel user; it was left untouched"
                        )
                        continue

                    await self.subscription_dao.rewrite_remna_id(
                        row.subscription_id,
                        encode_remna_id(panel_id),
                    )
                    resolved += 1

                # The unit of work only rolls back on its own; committing is the
                # caller's job. Without this every rewrite above is discarded
                # when the session closes, while the log still reports success.
                await self.uow.commit()
        else:
            logger.warning(
                f"Remnawave identity backfill stopped after {_MAX_BATCHES} batches; "
                f"the remaining rows are picked up on the next start"
            )

        report = BackfillReport(resolved=resolved, unresolved=unresolved)

        if report.total:
            logger.info(
                f"Remnawave identity backfill finished: {report.resolved} re-keyed, "
                f"{report.unresolved} left for manual review"
            )
        else:
            logger.debug("Remnawave identity backfill: nothing to do")

        return report
