"""Reaching the panel at startup, and when it is not there yet.

Right after a panel upgrade the panel is often still running its own migrations
when the bot starts. The identity backfill can only run once the panel answers,
so a single failed handshake at startup must not mean it never runs at all.
"""

import asyncio
from typing import Any, List

import pytest
from packaging.version import Version

from src.lifespan import connect_panel, retry_panel


class FakeRemnawave:
    def __init__(self, failures: int, version: str = "3.4.2") -> None:
        self.failures = failures
        self.version = version
        self.attempts = 0

    async def try_connection(self) -> Any:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ConnectionError("panel is still starting")
        return Version(self.version)


class FakeBackfill:
    def __init__(self) -> None:
        self.runs = 0

    async def run(self) -> None:
        self.runs += 1


class FakeScope:
    def __init__(self, deps: dict) -> None:
        self.deps = deps

    async def get(self, kind: Any) -> Any:
        return self.deps[kind.__name__]


class FakeContainer:
    """Stands in for dishka: every call opens a fresh request scope."""

    def __init__(self, remnawave: FakeRemnawave, backfill: FakeBackfill) -> None:
        self.deps = {"Remnawave": remnawave, "BackfillRemnaIdentity": backfill}
        self.scopes_opened = 0

    def __call__(self, scope: Any = None) -> "FakeContainer":
        return self

    async def __aenter__(self) -> FakeScope:
        self.scopes_opened += 1
        return FakeScope(self.deps)

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConfig:
    class build:  # noqa: N801
        data: dict = {}


class RecordingBus:
    def __init__(self) -> None:
        self.published: List[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


class TestConnectPanel:
    async def test_the_backfill_runs_after_a_successful_handshake(self) -> None:
        panel, backfill = FakeRemnawave(failures=0), FakeBackfill()

        await connect_panel(FakeContainer(panel, backfill), FakeConfig(), RecordingBus())  # type: ignore[arg-type]

        assert backfill.runs == 1

    async def test_the_backfill_does_not_run_if_the_panel_is_unreachable(self) -> None:
        # Resolving legacy rows needs to know which contract the panel speaks.
        panel, backfill = FakeRemnawave(failures=1), FakeBackfill()

        with pytest.raises(ConnectionError):
            await connect_panel(FakeContainer(panel, backfill), FakeConfig(), RecordingBus())  # type: ignore[arg-type]

        assert backfill.runs == 0

    async def test_it_works_in_a_scope_of_its_own(self) -> None:
        # The startup scope is closed by the time the handshake happens; the
        # backfill needs a live session to commit through.
        panel, backfill = FakeRemnawave(failures=0), FakeBackfill()
        container = FakeContainer(panel, backfill)

        await connect_panel(container, FakeConfig(), RecordingBus())  # type: ignore[arg-type]

        assert container.scopes_opened == 1

    async def test_a_panel_ahead_of_the_verified_range_is_reported(self) -> None:
        panel, backfill = FakeRemnawave(failures=0, version="3.6.0"), FakeBackfill()
        bus = RecordingBus()

        await connect_panel(FakeContainer(panel, backfill), FakeConfig(), bus)  # type: ignore[arg-type]

        assert len(bus.published) == 1

    async def test_a_verified_panel_raises_no_warning(self) -> None:
        panel, backfill = FakeRemnawave(failures=0, version="3.4.2"), FakeBackfill()
        bus = RecordingBus()

        await connect_panel(FakeContainer(panel, backfill), FakeConfig(), bus)  # type: ignore[arg-type]

        assert bus.published == []


class TestRetryPanel:
    async def test_it_keeps_trying_until_the_panel_answers(self) -> None:
        # Three failed handshakes, as if the panel took a while to migrate.
        panel, backfill = FakeRemnawave(failures=3), FakeBackfill()

        await retry_panel(
            FakeContainer(panel, backfill), FakeConfig(), RecordingBus(), interval=0  # type: ignore[arg-type]
        )

        assert panel.attempts == 4
        assert backfill.runs == 1

    async def test_it_stops_once_it_has_succeeded(self) -> None:
        panel, backfill = FakeRemnawave(failures=0), FakeBackfill()

        await retry_panel(
            FakeContainer(panel, backfill), FakeConfig(), RecordingBus(), interval=0  # type: ignore[arg-type]
        )

        assert panel.attempts == 1

    async def test_it_can_be_cancelled_at_shutdown(self) -> None:
        panel, backfill = FakeRemnawave(failures=10**9), FakeBackfill()
        task = asyncio.create_task(
            retry_panel(FakeContainer(panel, backfill), FakeConfig(), RecordingBus(), interval=0.01)  # type: ignore[arg-type]
        )

        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert backfill.runs == 0
