"""Strict HWID slots, against a stubbed 3.x panel.

The plan's limit is a ceiling; the panel holds what is occupied. These pin the
arithmetic, the temporary slot's life cycle, and the two ways it can go wrong
that cost real money: a panel error read as "no devices", and the panel's
physical limit leaking back into the plan's ceiling.
"""

import time
from typing import Any, Dict, List, Optional

import httpx
import pytest
from conftest import PANEL_ID, PanelStub, device_payload, user_payload
from test_identity_invariants import parsed_user
from test_service_end_to_end import a_plan, a_subscription, a_user, metadata

from src.application.dto import RemnaSubscriptionDto, SettingsDto
from src.application.events.system import UserDeviceAddedEvent, UserDeviceDeletedEvent
from src.application.use_cases.remnawave.commands.synchronization import SyncRemnaUser
from src.infrastructure.services.s.remnawave import SRemnawave
from src.infrastructure.services.s.slot_grants import SlotGrant, physical_limit, shown_counters
from src.infrastructure.services.s.slots import SlotOutcome, SlotService

DEVICES = f"/api/hwid/devices/{PANEL_ID}"


class FakeRedis:
    def __init__(self) -> None:
        self.data: Dict[str, str] = {}

    async def get(self, name: str) -> Optional[str]:
        return self.data.get(name)

    async def set(self, name: str, value: str, ex: Optional[int] = None, nx: bool = False) -> bool:
        self.data[name] = value
        return True

    async def delete(self, *names: str) -> int:
        return sum(1 for n in names if self.data.pop(n, None) is not None)

    async def exists(self, *names: str) -> int:
        return sum(1 for n in names if n in self.data)

    async def scan_iter(self, match: str) -> Any:
        prefix = match.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key


class FakeSettingsDao:
    def __init__(self, enabled: bool = True, base_limit: int = 1) -> None:
        self.settings = SettingsDto()
        self.settings.extra.s.slots.enabled = enabled
        self.settings.extra.s.slots.base_limit = base_limit

    async def get(self) -> SettingsDto:
        return self.settings


class FakeRequestScope:
    def __init__(self, dao: FakeSettingsDao) -> None:
        self.dao = dao

    async def __aenter__(self) -> "FakeRequestScope":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, kind: Any) -> FakeSettingsDao:
        return self.dao


class FakeContainer:
    def __init__(self, dao: FakeSettingsDao) -> None:
        self.dao = dao

    def __call__(self) -> FakeRequestScope:
        return FakeRequestScope(self.dao)


class FakeSubscriptionDao:
    def __init__(self, subscription: Any) -> None:
        self.subscription = subscription

    async def get_current(self, user_id: int) -> Any:
        return self.subscription

    async def get_by_remna_id(self, remna_id: Any) -> Any:
        return self.subscription


class FakeUserDao:
    async def get_by_telegram_id(self, telegram_id: int) -> Any:
        return a_user()

    async def get_with_active_subscription(self) -> List[Any]:
        return [a_user()]


class RecordingNotifier:
    def __init__(self) -> None:
        self.keys: List[str] = []

    async def notify_user(self, user: Any, payload: Any = None, i18n_key: Any = None) -> None:
        self.keys.append(payload.i18n_key)


def devices(panel: PanelStub, count: int, *, status: int = 200) -> None:
    items = [device_payload(line="v3", hwid=f"HW-{i}") for i in range(count)]
    panel.on("GET", DEVICES, {"total": count, "devices": items}, status=status)


def limits_sent(panel: PanelStub) -> List[int]:
    return [c.body["hwidDeviceLimit"] for c in panel.calls if c.method == "PATCH" and c.body]


async def a_client(
    sdk: Any, http: httpx.AsyncClient, panel: PanelStub, settings: FakeSettingsDao, redis: FakeRedis
) -> SRemnawave:
    panel.on("GET", "/api/system/metadata", metadata("3.4.5"))
    panel.on("PATCH", "/api/users", user_payload(line="v3"))
    panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3", hwidDeviceLimit=3))
    client = SRemnawave(sdk, http, FakeContainer(settings), redis)  # type: ignore[arg-type]
    await client.try_connection()
    return client


def a_service(
    client: SRemnawave, settings: FakeSettingsDao, redis: FakeRedis, subscription: Any
) -> tuple[SlotService, RecordingNotifier]:
    notifier = RecordingNotifier()
    service = SlotService(
        redis,  # type: ignore[arg-type]
        client,
        settings,  # type: ignore[arg-type]
        FakeSubscriptionDao(subscription),  # type: ignore[arg-type]
        FakeUserDao(),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
    )
    return service, notifier


def device_event(cls: Any) -> Any:
    return cls(
        user_id=7,
        telegram_id=123,
        username=None,
        name="Test",
        email=None,
        hwid="HW-9",
        platform="android",
        device_model="Pixel",
        os_version="14",
        user_agent="Happ/1.0",
    )


class TestArithmetic:
    @pytest.mark.parametrize(
        "ceiling, occupied, base, granted, expected",
        [
            (3, 0, 1, None, 1),  # a new user can connect the first device
            (3, 2, 1, None, 2),  # no free place beyond what is taken
            (3, 2, 1, 3, 3),  # an open slot adds one
            (2, 3, 1, None, 3),  # a lowered plan does not disconnect anyone
            (1, 0, 2, None, 1),  # the base never exceeds the plan
            (0, 5, 1, None, 0),  # unlimited plans are left alone
        ],
    )
    def test_physical_limit(
        self, ceiling: int, occupied: int, base: int, granted: Optional[int], expected: int
    ) -> None:
        assert physical_limit(ceiling, occupied, base, granted) == expected

    def test_the_shown_limit_never_reads_below_occupied(self) -> None:
        assert shown_counters(ceiling=2, occupied=3) == (3, 3)
        assert shown_counters(ceiling=5, occupied=2) == (2, 5)


class TestPanelWrites:
    async def test_a_renewal_writes_the_occupied_count_not_the_plan(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)

        await client.update_user(
            a_user(), a_subscription().user_remna_id, plan=a_plan(device_limit=5)
        )

        assert limits_sent(panel) == [2]

    async def test_an_open_slot_survives_a_renewal(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=5))
        await service.open_slot(a_user(), a_subscription(device_limit=5))

        await client.update_user(
            a_user(), a_subscription().user_remna_id, plan=a_plan(device_limit=5)
        )

        assert limits_sent(panel) == [3, 3]

    async def test_a_panel_error_leaves_the_limit_alone(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # Neither the full ceiling nor a guess: the limit is simply not sent.
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 0, status=500)

        await client.update_user(
            a_user(), a_subscription().user_remna_id, plan=a_plan(device_limit=5)
        )

        patch = [c for c in panel.calls if c.method == "PATCH"][-1]
        assert "hwidDeviceLimit" not in patch.body

    async def test_off_means_the_plan_limit_as_before(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(enabled=False), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)

        await client.update_user(
            a_user(), a_subscription().user_remna_id, plan=a_plan(device_limit=5)
        )

        assert limits_sent(panel) == [5]
        assert not [c for c in panel.calls if c.path == DEVICES]

    async def test_a_new_user_starts_at_the_base(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(base_limit=1), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        panel.on("POST", "/api/users", user_payload(line="v3"))

        await client.create_user(a_user(), plan=a_plan(device_limit=5))

        create = [c for c in panel.calls if c.method == "POST"][-1]
        assert create.body["hwidDeviceLimit"] == 1


class TestTemporarySlot:
    async def test_opening_raises_the_panel_limit_by_one(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=5))

        result = await service.open_slot(a_user(), a_subscription(device_limit=5))

        assert result.outcome == SlotOutcome.OPENED
        assert limits_sent(panel) == [3]

    async def test_a_second_press_reports_the_open_slot(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=5))
        await service.open_slot(a_user(), a_subscription(device_limit=5))

        again = await service.open_slot(a_user(), a_subscription(device_limit=5))

        assert again.outcome == SlotOutcome.ALREADY_OPEN
        assert limits_sent(panel) == [3]

    async def test_the_ceiling_stops_the_user_but_not_an_admin(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 3)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=3))

        user = await service.open_slot(a_user(), a_subscription(device_limit=3))
        admin = await service.open_slot(a_user(), a_subscription(device_limit=3), by_admin=True)

        assert user.outcome == SlotOutcome.AT_CEILING
        assert admin.outcome == SlotOutcome.OPENED
        assert limits_sent(panel) == [4]

    async def test_with_a_free_place_there_is_nothing_to_open(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(base_limit=1), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 0)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=3))

        result = await service.open_slot(a_user(), a_subscription(device_limit=3))

        assert result.outcome == SlotOutcome.FREE_PLACE
        assert limits_sent(panel) == []

    async def test_an_unanswered_device_list_opens_nothing(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 0, status=500)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=3))

        result = await service.open_slot(a_user(), a_subscription(device_limit=3))

        assert result.outcome == SlotOutcome.UNAVAILABLE
        assert limits_sent(panel) == []

    async def test_an_unused_slot_is_taken_back_when_it_expires(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)
        subscription = a_subscription(device_limit=5)
        service, notifier = a_service(client, settings, redis, subscription)
        await service.open_slot(a_user(), subscription)
        grant = await service.get_grant(subscription.user_remna_id)
        assert grant is not None
        grant.expires_at = time.time() - 1
        await service._save_grant(subscription.user_remna_id, grant)

        assert await service.close_expired() == 1

        assert limits_sent(panel) == [3, 2]
        assert notifier.keys == ["ntf-s-slots.expired"]
        assert await service.get_grant(subscription.user_remna_id) is None

    async def test_a_device_arriving_closes_the_slot_at_once(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)
        subscription = a_subscription(device_limit=5)
        service, notifier = a_service(client, settings, redis, subscription)
        await service.open_slot(a_user(), subscription)
        devices(panel, 3)

        await service.on_device_added(device_event(UserDeviceAddedEvent))

        assert limits_sent(panel) == [3, 3]
        assert notifier.keys == ["ntf-s-slots.consumed"]
        assert await service.get_grant(subscription.user_remna_id) is None

    async def test_a_panel_failure_on_expiry_keeps_the_grant_for_later(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        subscription = a_subscription(device_limit=5)
        service, _ = a_service(client, settings, redis, subscription)
        await service._save_grant(
            subscription.user_remna_id,
            SlotGrant(
                telegram_id=1,
                original_limit=2,
                granted_limit=3,
                device_count_before=2,
                expires_at=time.time() - 1,
            ),
        )
        devices(panel, 0, status=500)

        assert await service.close_expired() == 0

        grant = await service.get_grant(subscription.user_remna_id)
        assert grant is not None and grant.misses == 1
        assert limits_sent(panel) == []


class TestReconcile:
    async def test_a_deleted_device_frees_no_place_in_the_panel(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 1)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=5))

        await service.on_device_deleted(device_event(UserDeviceDeletedEvent))

        assert limits_sent(panel) == [1]

    async def test_an_open_slot_is_not_swept(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 2)
        subscription = a_subscription(device_limit=5)
        service, _ = a_service(client, settings, redis, subscription)
        await service.open_slot(a_user(), subscription)

        assert await service.sweep() == 0
        assert limits_sent(panel) == [3]

    async def test_an_unanswered_device_list_is_not_read_as_empty(
        self, sdk: Any, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        settings, redis = FakeSettingsDao(), FakeRedis()
        client = await a_client(sdk, http, panel, settings, redis)
        devices(panel, 0, status=500)
        service, _ = a_service(client, settings, redis, a_subscription(device_limit=5))

        assert await service.sweep() == 0
        assert limits_sent(panel) == []


class TestCeilingSurvivesPanelSync:
    async def test_the_plans_limit_is_not_overwritten_by_the_physical_one(self) -> None:
        class RecordingSubscriptionDao:
            updated: Any = None

            async def update(self, subscription: Any) -> None:
                self.updated = subscription

        class RemnawaveForSync:
            def apply_sync(self, target: Any, source: Any) -> Any:
                target.device_limit = source.device_limit
                return target

        sync = SyncRemnaUser.__new__(SyncRemnaUser)
        sync.subscription_dao = RecordingSubscriptionDao()  # type: ignore[assignment]
        sync.remnawave = RemnawaveForSync()  # type: ignore[assignment]
        sync.settings_dao = FakeSettingsDao(enabled=True)  # type: ignore[assignment]

        local = a_subscription(device_limit=5)
        panel_side = RemnaSubscriptionDto.from_remna_user(parsed_user("v3", hwidDeviceLimit=2))

        await sync._update_subscription(local, panel_side)

        assert sync.subscription_dao.updated.device_limit == 5  # type: ignore[attr-defined]
