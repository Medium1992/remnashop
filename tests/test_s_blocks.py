"""Temporary blocks: off in the panel, told why, back on when time is up."""

from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest
from test_service_end_to_end import a_subscription

from src.application.common import TranslatorRunner
from src.application.common.interactor import SYSTEM_ACTOR
from src.application.dto import TelegramUserDto, UserDto
from src.application.dto.s_blocks import UserBlockDto
from src.core.enums import Role, SubscriptionStatus
from src.core.exceptions import PermissionDeniedError
from src.core.utils.time import datetime_now
from src.infrastructure.services.s.blocks import BlockService
from src.telegram.routers.s.block.handlers import on_block_button, torrent_report_keyboard


class FakeUow:
    commits = 0

    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class FakeBlockDao:
    def __init__(self) -> None:
        self.rows: Dict[int, UserBlockDto] = {}

    async def get(self, user_id: int) -> Optional[UserBlockDto]:
        return self.rows.get(user_id)

    async def put(self, block: UserBlockDto) -> None:
        self.rows[block.user_id] = block

    async def remove(self, user_id: int) -> bool:
        return self.rows.pop(user_id, None) is not None

    async def due(self, now: Any) -> List[UserBlockDto]:
        return [b for b in self.rows.values() if b.until and b.until <= now]


class FakeUserDao:
    async def get_by_id(self, user_id: int) -> UserDto:
        return UserDto(id=user_id, telegram_id=1000 + user_id, name="Test")


class FakeSubscriptionDao:
    def __init__(self) -> None:
        self.subscription = a_subscription()
        self.subscription.id = 70

    async def get_current(self, user_id: int) -> Any:
        return self.subscription

    async def update_status(self, subscription_id: int, status: SubscriptionStatus) -> None:
        self.subscription.status = status


class FakePanel:
    def __init__(self, fail: bool = False) -> None:
        self.calls: List[str] = []
        self.fail = fail

    async def _call(self, name: str) -> None:
        if self.fail:
            raise RuntimeError("panel down")
        self.calls.append(name)

    async def disable_user(self, uuid: Any) -> None:
        await self._call("disable")

    async def enable_user(self, uuid: Any) -> None:
        await self._call("enable")

    async def drop_connections(self, uuid: Any) -> None:
        await self._call("drop")


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    async def notify_user(self, user: Any, payload: Any = None, i18n_key: Any = None) -> None:
        self.sent.append(payload)


def build(panel: Optional[FakePanel] = None) -> tuple[BlockService, Any, Any, Any, Any]:
    dao, subs, panel, notifier = (
        FakeBlockDao(),
        FakeSubscriptionDao(),
        panel or FakePanel(),
        RecordingNotifier(),
    )
    service = BlockService(
        FakeUow(),  # type: ignore[arg-type]
        dao,  # type: ignore[arg-type]
        FakeUserDao(),  # type: ignore[arg-type]
        subs,  # type: ignore[arg-type]
        panel,  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
    )
    return service, dao, subs, panel, notifier


ADMIN = UserDto(id=1, telegram_id=1, name="Admin", role=Role.OWNER)


class TestBlock:
    async def test_a_block_switches_off_drops_and_tells(self) -> None:
        service, dao, subs, panel, notifier = build()

        block = await service.block(ADMIN, 7, hours=24, reason="торренты")

        assert panel.calls == ["disable", "drop"]
        assert subs.subscription.status == SubscriptionStatus.DISABLED
        assert dao.rows[7].reason == "торренты"
        assert block.until is not None
        assert notifier.sent[0].i18n_key == "ntf-s-blocks.blocked"
        assert notifier.sent[0].i18n_kwargs["until"].endswith("UTC")

    async def test_without_hours_it_lasts_until_lifted(self) -> None:
        service, dao, *_ = build()
        block = await service.block(ADMIN, 7, hours=None)
        assert block.is_permanent
        assert dao.rows[7].reason == ""

    async def test_lifting_switches_back_on(self) -> None:
        service, dao, subs, panel, notifier = build()
        await service.block(ADMIN, 7, hours=1)

        assert await service.unblock(ADMIN, 7)

        assert panel.calls[-1] == "enable"
        assert subs.subscription.status == SubscriptionStatus.ACTIVE
        assert 7 not in dao.rows
        assert notifier.sent[-1].i18n_key == "ntf-s-blocks.unblocked"

    async def test_a_plain_user_may_not_block(self) -> None:
        service, dao, *_ = build()
        with pytest.raises(PermissionDeniedError):
            await service.block(UserDto(id=2, telegram_id=2, name="U"), 7, hours=1)
        assert dao.rows == {}

    async def test_lifting_nothing_is_false(self) -> None:
        service, *_ = build()
        assert not await service.unblock(ADMIN, 7)


class TestRelease:
    async def test_only_blocks_whose_time_is_up(self) -> None:
        service, dao, *_ = build()
        dao.rows[7] = UserBlockDto(user_id=7, until=datetime_now() - timedelta(minutes=1))
        dao.rows[8] = UserBlockDto(user_id=8, until=datetime_now() + timedelta(hours=1))
        dao.rows[9] = UserBlockDto(user_id=9, until=None)

        assert await service.release_due(SYSTEM_ACTOR) == 1
        assert set(dao.rows) == {8, 9}

    async def test_a_panel_failure_keeps_the_block_for_the_next_run(self) -> None:
        service, dao, subs, *_ = build(FakePanel(fail=True))
        subs.subscription.status = SubscriptionStatus.DISABLED
        dao.rows[7] = UserBlockDto(user_id=7, until=datetime_now() - timedelta(minutes=1))

        assert await service.release_due(SYSTEM_ACTOR) == 0
        assert 7 in dao.rows


class TestTorrentReportButtons:
    def test_the_card_comes_with_two_block_buttons(self) -> None:
        markup = torrent_report_keyboard(7)
        assert markup is not None
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert data[1:] == ["s_block:7:24", "s_block:7:menu"]

    def test_no_user_no_buttons(self) -> None:
        assert torrent_report_keyboard(None) is None


class _Container:
    def __init__(self, service: BlockService) -> None:
        self.objects: Dict[Any, Any] = {BlockService: service, TranslatorRunner: _I18n()}

    async def get(self, dependency_type: Any, component: Any = None) -> Any:
        return self.objects[dependency_type]


class _I18n:
    def get(self, key: str, **kwargs: Any) -> str:
        return key


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        pass


class _DialogManager:
    def __init__(self, container: _Container) -> None:
        self.started: List[Any] = []
        self.middleware_data = {"dishka_container": container}

    async def start(self, state: Any, **kwargs: Any) -> None:
        self.started.append(state)


class TestForgedBlockButtons:
    """The buttons sit under an admin alert, but their data can be sent by anyone."""

    async def _press(self, data: str, role: Role) -> tuple[Any, _DialogManager]:
        service, dao, *_ = build()
        manager = _DialogManager(_Container(service))
        user = TelegramUserDto(id=2, telegram_id=2, name="U", role=role)
        await on_block_button(_Callback(data), manager, user=user)  # type: ignore[arg-type,call-arg]
        return dao, manager

    async def test_a_user_does_not_get_the_block_dialog(self) -> None:
        _, manager = await self._press("s_block:7:menu", Role.USER)
        assert manager.started == []

    async def test_a_user_does_not_block(self) -> None:
        dao, _ = await self._press("s_block:7:24", Role.USER)
        assert dao.rows == {}

    async def test_an_admin_gets_both(self) -> None:
        _, manager = await self._press("s_block:7:menu", Role.ADMIN)
        assert len(manager.started) == 1
        dao, _ = await self._press("s_block:7:24", Role.ADMIN)
        assert 7 in dao.rows
