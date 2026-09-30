"""The real backfill, with the real unit of work and DAO, against real Postgres.

The unit-test fakes model transactions, but the property this guards is too
important to leave to a model: after the backfill returns, the rewritten keys
have to be visible from a *different* connection. A run that only staged its
writes would pass every check made through its own session and still leave the
table untouched.

Needs a scratch database with the migrations applied; see README.md.
"""

import asyncio
import os
import sys
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.use_cases.remnawave.commands.identity import BackfillRemnaIdentity
from src.core.utils.remna_id import decode_remna_id, encode_remna_id
from src.infrastructure.database.dao.subscription import SubscriptionDaoImpl
from src.infrastructure.database.uow import UnitOfWorkImpl

DSN = (
    f"postgresql+asyncpg://{os.environ['DATABASE_USER']}:"
    f"{os.environ['DATABASE_PASSWORD']}@{os.environ['DATABASE_HOST']}:"
    f"{os.environ['DATABASE_PORT']}/{os.environ['DATABASE_NAME']}"
)

# 450 legacy rows: enough for three batches. Every tenth one cannot be matched.
ROWS = 450
UNMATCHED = {i for i in range(1, ROWS + 1) if i % 10 == 0}

failures: List[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'OK  ' if condition else 'FAIL'} {label} {'' if condition else detail}")
    if not condition:
        failures.append(label)


def legacy_key(i: int) -> UUID:
    return UUID(int=(1 << 100) + i)


class Panel:
    async def resolve_legacy_identity(
        self,
        stored: UUID,
        subscription_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[int]:
        i = stored.int - (1 << 100)
        return None if i in UNMATCHED else 100_000 + i


async def seed(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM subscriptions"))
        await conn.execute(text("DELETE FROM users"))
        await conn.execute(
            text(
                "INSERT INTO users (id, telegram_id, name, referral_code, role, language,"
                " auth_type, personal_discount, purchase_discount, is_blocked,"
                " is_bot_blocked, is_rules_accepted, is_trial_available,"
                " is_email_verified, created_at, updated_at) VALUES"
                " (1, 111, 'A', 'ref1', 'USER', 'EN', 'TELEGRAM', 0, 0,"
                " false, false, false, true, false, now(), now())"
            )
        )
        for i in range(1, ROWS + 1):
            await conn.execute(
                text(
                    "INSERT INTO subscriptions (id, user_remna_id, user_id, status,"
                    " is_trial, disabled_by_channel_leave, traffic_limit, device_limit,"
                    " traffic_limit_strategy, internal_squads, expire_at, url,"
                    " plan_snapshot, created_at, updated_at) VALUES"
                    " (:id, :key, 1, 'ACTIVE', false, false, 100, 3, 'NO_RESET',"
                    " '{}', now(), :url, '{}', now(), now())"
                ),
                {"id": i, "key": legacy_key(i), "url": f"https://s.example/short{i:020d}"},
            )


async def run_backfill(engine) -> None:  # type: ignore[no-untyped-def]
    # Wired exactly as the container wires it, on a session of its own.
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        dao = SubscriptionDaoImpl.__new__(SubscriptionDaoImpl)
        dao.session = session
        backfill = BackfillRemnaIdentity(UnitOfWorkImpl(session), dao, Panel())  # type: ignore[arg-type]
        report = await backfill.run()

    check("the report counts every row", report.total == ROWS, f"got {report.total}")
    check(
        "the report splits them correctly",
        (report.resolved, report.unresolved) == (ROWS - len(UNMATCHED), len(UNMATCHED)),
        f"got {(report.resolved, report.unresolved)}",
    )


async def read_back(engine) -> Dict[int, UUID]:  # type: ignore[no-untyped-def]
    # A brand-new connection: only committed data is visible here.
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT id, user_remna_id FROM subscriptions"))
        return {row.id: row.user_remna_id for row in rows}


async def main() -> None:
    engine = create_async_engine(DSN)
    await seed(engine)
    await run_backfill(engine)
    stored = await read_back(engine)

    rekeyed = {i for i, key in stored.items() if decode_remna_id(key) is not None}
    check(
        "the rewrites are committed and visible from another connection",
        rekeyed == set(range(1, ROWS + 1)) - UNMATCHED,
        f"{len(rekeyed)} re-keyed",
    )
    check(
        "every matched row carries its own panel id",
        all(stored[i] == encode_remna_id(100_000 + i) for i in rekeyed),
    )
    check(
        "every unmatched row is left exactly as it was",
        all(stored[i] == legacy_key(i) for i in UNMATCHED),
    )

    # Run it again: nothing left to do, and nothing moves.
    before = dict(stored)
    await run_backfill_again(engine)
    check("a second run changes nothing", await read_back(engine) == before)

    await engine.dispose()

    if failures:
        sys.exit(1)
    print("\nThe backfill persists its work.")


async def run_backfill_again(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        dao = SubscriptionDaoImpl.__new__(SubscriptionDaoImpl)
        dao.session = session
        report = await BackfillRemnaIdentity(UnitOfWorkImpl(session), dao, Panel()).run()  # type: ignore[arg-type]
    check(
        "the second run only revisits the unmatched rows",
        (report.resolved, report.unresolved) == (0, len(UNMATCHED)),
        f"got {(report.resolved, report.unresolved)}",
    )


asyncio.run(main())
