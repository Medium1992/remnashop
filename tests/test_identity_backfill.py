"""The one-off re-keying of rows written before the panel dropped users.uuid.

This touches production data, so the properties that matter most are the ones
about *not* doing damage: a row that cannot be matched is left exactly as it
was, a row already carrying an encoded id is never revisited, and running the
whole thing twice changes nothing the second time.
"""

from typing import Dict, List, Optional
from uuid import UUID

import pytest

from src.application.dto import LegacyIdentityDto
from src.application.use_cases.remnawave.commands.identity import BackfillRemnaIdentity
from src.core.utils.remna_id import decode_remna_id, encode_remna_id
from src.infrastructure.services.remnawave._identity import short_uuid_from_url

LEGACY_A = UUID("6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c")
LEGACY_B = UUID("11112222-3333-4444-5555-666677778888")


class FakeSubscriptionDao:
    """A table with transactions, modelled on the real unit of work.

    Writes land in a pending set that reads within the same transaction can
    see, and only reach the table on commit. Leaving the unit of work without
    committing discards them -- which is what ``UnitOfWorkImpl.__aexit__`` does,
    since it rolls back on error and otherwise leaves committing to the caller.
    """

    def __init__(self, rows: Dict[int, UUID], urls: Optional[Dict[int, str]] = None) -> None:
        self.rows = dict(rows)
        self.pending: Dict[int, UUID] = {}
        self.urls = urls or {}
        self.reads = 0

    def _visible(self) -> Dict[int, UUID]:
        return {**self.rows, **self.pending}

    async def get_legacy_identities(
        self, limit: int, after_id: int = 0
    ) -> List[LegacyIdentityDto]:
        self.reads += 1
        legacy = [
            LegacyIdentityDto(
                subscription_id=sub_id,
                user_remna_id=key,
                url=self.urls.get(sub_id, f"https://sub.example.com/short{sub_id:020d}"),
                username=f"rs_{sub_id}",
            )
            for sub_id, key in sorted(self._visible().items())
            if decode_remna_id(key) is None and sub_id > after_id
        ]
        return legacy[:limit]

    async def rewrite_remna_id(self, subscription_id: int, remna_id: UUID) -> None:
        self.pending[subscription_id] = remna_id


class FakeUow:
    def __init__(self, dao: FakeSubscriptionDao) -> None:
        self.dao = dao
        self.commits = 0

    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *args: object) -> None:
        # Mirrors UnitOfWorkImpl: nothing is committed implicitly.
        self.dao.pending.clear()

    async def commit(self) -> None:
        self.dao.rows.update(self.dao.pending)
        self.dao.pending.clear()
        self.commits += 1


class FakeRemnawave:
    """Resolves a legacy uuid the way the panel would, per the given mapping."""

    def __init__(self, by_uuid: Optional[Dict[UUID, int]] = None) -> None:
        self.by_uuid = by_uuid or {}
        self.calls: List[UUID] = []

    async def resolve_legacy_identity(
        self,
        stored: UUID,
        subscription_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[int]:
        self.calls.append(stored)
        return self.by_uuid.get(stored)


def make(dao: FakeSubscriptionDao, panel: FakeRemnawave) -> BackfillRemnaIdentity:
    return BackfillRemnaIdentity(FakeUow(dao), dao, panel)  # type: ignore[arg-type]


class TestRekeying:
    async def test_a_legacy_row_is_rewritten_to_the_encoded_id(self) -> None:
        dao = FakeSubscriptionDao({1: LEGACY_A})
        panel = FakeRemnawave({LEGACY_A: 18465})

        report = await make(dao, panel).run()

        assert dao.rows[1] == encode_remna_id(18465)
        assert report.resolved == 1
        assert report.unresolved == 0

    async def test_several_rows_are_each_given_their_own_id(self) -> None:
        dao = FakeSubscriptionDao({1: LEGACY_A, 2: LEGACY_B})
        panel = FakeRemnawave({LEGACY_A: 100, LEGACY_B: 200})

        await make(dao, panel).run()

        assert decode_remna_id(dao.rows[1]) == 100
        assert decode_remna_id(dao.rows[2]) == 200

    async def test_an_already_encoded_row_is_never_touched(self) -> None:
        # It is not legacy, so it must not even be read, let alone rewritten.
        encoded = encode_remna_id(777)
        dao = FakeSubscriptionDao({1: encoded})
        panel = FakeRemnawave()

        report = await make(dao, panel).run()

        assert dao.rows[1] == encoded
        assert panel.calls == []
        assert report.total == 0


class TestNotDoingDamage:
    async def test_an_unresolvable_row_keeps_its_original_value(self) -> None:
        # Guessing here would point a live subscription at someone else.
        dao = FakeSubscriptionDao({1: LEGACY_A})
        panel = FakeRemnawave({})

        report = await make(dao, panel).run()

        assert dao.rows[1] == LEGACY_A
        assert report.resolved == 0
        assert report.unresolved == 1

    async def test_one_unresolvable_row_does_not_block_the_others(self) -> None:
        dao = FakeSubscriptionDao({1: LEGACY_A, 2: LEGACY_B})
        panel = FakeRemnawave({LEGACY_B: 200})

        report = await make(dao, panel).run()

        assert dao.rows[1] == LEGACY_A
        assert decode_remna_id(dao.rows[2]) == 200
        assert (report.resolved, report.unresolved) == (1, 1)

    async def test_an_unresolvable_row_does_not_spin_forever(self) -> None:
        # It stays legacy, so it comes back in the next slice; the run has to
        # notice it has already been seen and stop.
        dao = FakeSubscriptionDao({1: LEGACY_A})
        panel = FakeRemnawave({})

        await make(dao, panel).run()

        assert dao.reads <= 3
        assert len(panel.calls) == 1


class TestIdempotence:
    async def test_a_second_run_changes_nothing(self) -> None:
        dao = FakeSubscriptionDao({1: LEGACY_A, 2: LEGACY_B})
        panel = FakeRemnawave({LEGACY_A: 100, LEGACY_B: 200})

        await make(dao, panel).run()
        snapshot = dict(dao.rows)

        second = await make(dao, FakeRemnawave({LEGACY_A: 999})).run()

        assert dao.rows == snapshot
        assert second.total == 0

    async def test_an_empty_install_does_nothing(self) -> None:
        dao = FakeSubscriptionDao({})
        panel = FakeRemnawave()

        report = await make(dao, panel).run()

        assert report.total == 0
        assert panel.calls == []


class TestShortUuidRecovery:
    """The 3.x path leans on the shortUuid stored inside the subscription url."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://sub.example.com/abcdef0123456789abcd", "abcdef0123456789abcd"),
            ("https://sub.example.com/abcdef0123456789abcd/", "abcdef0123456789abcd"),
            ("https://sub.example.com/sub/abcdef0123456789abcd", "abcdef0123456789abcd"),
            ("https://sub.example.com/abcdef0123456789abcd?x=1", "abcdef0123456789abcd"),
        ],
    )
    def test_the_short_uuid_is_recovered_from_a_stored_url(
        self, url: str, expected: str
    ) -> None:
        assert short_uuid_from_url(url) == expected

    @pytest.mark.parametrize("url", ["", None, "https://sub.example.com/", "not a url"])
    def test_a_url_with_no_short_uuid_yields_nothing(self, url: Optional[str]) -> None:
        # Better to report the row as unresolved than to send the panel garbage.
        assert short_uuid_from_url(url) is None


class TestPersistence:
    """The rewrites have to survive the end of the unit of work."""

    async def test_a_rewrite_is_committed_not_just_staged(self) -> None:
        # The unit of work does not commit on exit. A run that only staged its
        # writes would report success while leaving the table untouched.
        dao = FakeSubscriptionDao({1: LEGACY_A})
        panel = FakeRemnawave({LEGACY_A: 18465})

        await make(dao, panel).run()

        assert dao.pending == {}
        assert dao.rows[1] == encode_remna_id(18465)

    async def test_every_batch_is_committed(self) -> None:
        # One transaction per slice, so a crash midway keeps what was done.
        rows = {i: UUID(int=(1 << 100) + i) for i in range(1, 451)}
        dao = FakeSubscriptionDao(rows)
        panel = FakeRemnawave({key: i for i, key in rows.items()})
        uow = FakeUow(dao)

        await BackfillRemnaIdentity(uow, dao, panel).run()  # type: ignore[arg-type]

        assert uow.commits == 3  # 200 + 200 + 50
        assert all(decode_remna_id(key) == i for i, key in dao.rows.items())


class TestLargeInstalls:
    async def test_rows_behind_many_unresolvable_ones_are_still_reached(self) -> None:
        # 250 rows nobody can match, then one that can be. Re-reading "the
        # first 200 legacy rows" would return the same unmatched rows forever
        # and never get to the last one.
        rows = {i: UUID(int=(1 << 100) + i) for i in range(1, 252)}
        dao = FakeSubscriptionDao(rows)
        panel = FakeRemnawave({rows[251]: 999})

        report = await make(dao, panel).run()

        assert decode_remna_id(dao.rows[251]) == 999
        assert report.resolved == 1
        assert report.unresolved == 250

    async def test_every_legacy_row_is_attempted_exactly_once(self) -> None:
        rows = {i: UUID(int=(1 << 100) + i) for i in range(1, 501)}
        dao = FakeSubscriptionDao(rows)
        panel = FakeRemnawave({})

        await make(dao, panel).run()

        assert sorted(panel.calls) == sorted(rows.values())
