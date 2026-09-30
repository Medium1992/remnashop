"""The backfill's selection, run as real SQL against a real Postgres.

Everything else about the backfill has been tested against a fake DAO. What that
cannot check is the part the migration story actually rests on: whether Postgres
orders the uuid column the way the encoding assumes, so that
`user_remna_id >= SYNTHETIC_UPPER_BOUND` really separates rows written by an
older RemnaShop from rows this build wrote.
"""

import asyncio
import os
import sys
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.utils.remna_id import SYNTHETIC_UPPER_BOUND, encode_remna_id

DSN = (
    f"postgresql+asyncpg://{os.environ['DATABASE_USER']}:"
    f"{os.environ['DATABASE_PASSWORD']}@{os.environ['DATABASE_HOST']}:"
    f"{os.environ['DATABASE_PORT']}/{os.environ['DATABASE_NAME']}"
)

LEGACY = [
    UUID("6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c"),
    UUID("00000000-0000-0000-0001-000000000000"),  # exactly the boundary
    UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
    UUID("00000000-0000-0000-0002-000000000000"),
]
SYNTHETIC = [encode_remna_id(i) for i in (1, 42, 18465, 2**31, (1 << 48) - 1)]

failures = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"FAIL {label} {detail}")


async def main() -> None:
    engine = create_async_engine(DSN)

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
                " false, false, false, true, false, now(), now()),"
                " (2, NULL, 'B', 'ref2', 'USER', 'EN', 'EMAIL', 0, 0,"
                " false, false, false, true, false, now(), now())"
            )
        )

        rows = [(u, "legacy") for u in LEGACY] + [(u, "synthetic") for u in SYNTHETIC]
        for i, (key, kind) in enumerate(rows, start=1):
            await conn.execute(
                text(
                    "INSERT INTO subscriptions (id, user_remna_id, user_id, status,"
                    " is_trial, disabled_by_channel_leave, traffic_limit, device_limit,"
                    " traffic_limit_strategy, internal_squads, expire_at, url,"
                    " plan_snapshot, created_at, updated_at) VALUES"
                    " (:id, :key, 1, 'ACTIVE', false, false, 100, 3, 'NO_RESET',"
                    " '{}', now(), :url, '{}', now(), now())"
                ),
                {"id": i, "key": key, "url": f"https://sub.example.com/short{i:020d}"},
            )

    # -- the predicate the backfill selects on ---------------------------
    async with engine.connect() as conn:
        selected = (
            await conn.execute(
                text(
                    "SELECT user_remna_id FROM subscriptions"
                    " WHERE user_remna_id >= :bound ORDER BY id"
                ),
                {"bound": SYNTHETIC_UPPER_BOUND},
            )
        ).scalars().all()

    check(
        "legacy rows are the ones selected",
        set(selected) == set(LEGACY),
        f"got {sorted(map(str, selected))}",
    )
    check(
        "no synthetic row is selected",
        not (set(selected) & set(SYNTHETIC)),
    )

    # -- the SQL a future migration will run -----------------------------
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS panel_id bigint")
        )
        await conn.execute(text("UPDATE subscriptions SET panel_id = NULL"))
        await conn.execute(
            text(
                "UPDATE subscriptions SET panel_id ="
                " ('x' || right(user_remna_id::text, 12))::bit(48)::bigint"
                " WHERE user_remna_id < '00000000-0000-0000-0001-000000000000'"
            )
        )

    async with engine.connect() as conn:
        decoded = dict(
            (
                await conn.execute(
                    text(
                        "SELECT user_remna_id, panel_id FROM subscriptions"
                        " WHERE panel_id IS NOT NULL"
                    )
                )
            ).all()
        )
        untouched = (
            await conn.execute(
                text("SELECT count(*) FROM subscriptions WHERE panel_id IS NULL")
            )
        ).scalar_one()

    expected = {encode_remna_id(i): i for i in (1, 42, 18465, 2**31, (1 << 48) - 1)}
    check("every synthetic key decodes to its panel id", decoded == expected, f"got {decoded}")
    check("every legacy row is left for manual review", untouched == len(LEGACY))

    # -- the DAO method itself, not an equivalent query -------------------
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.infrastructure.database.dao.subscription import SubscriptionDaoImpl

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        dao = SubscriptionDaoImpl.__new__(SubscriptionDaoImpl)
        dao.session = session

        found = await dao.get_legacy_identities(limit=100)

        check(
            "the DAO returns exactly the legacy rows",
            {row.user_remna_id for row in found} == set(LEGACY),
            f"got {sorted(str(r.user_remna_id) for r in found)}",
        )
        check(
            "a telegram user gets the rs_<telegram_id> fallback name",
            all(r.username == "rs_111" for r in found),
            f"got {[r.username for r in found]}",
        )
        check(
            "the stored url is carried through for shortUuid recovery",
            all(r.url.startswith("https://sub.example.com/short") for r in found),
        )

        # A web-only account has no telegram id and falls back to rs_web_<id>.
        await session.execute(
            text("UPDATE subscriptions SET user_id = 2 WHERE id = 1")
        )
        await session.commit()

        web = [r for r in await dao.get_legacy_identities(limit=100) if r.subscription_id == 1]
        check(
            "a web-only account gets the rs_web_<id> fallback name",
            web and web[0].username == "rs_web_2",
            f"got {[r.username for r in web]}",
        )

        # The batch limit is honoured, which is what keeps the backfill bounded.
        check(
            "the limit bounds the batch",
            len(await dao.get_legacy_identities(limit=2)) == 2,
        )

    await engine.dispose()

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)

    print("\nThe backfill predicate and the migration SQL agree with Postgres.")


asyncio.run(main())
