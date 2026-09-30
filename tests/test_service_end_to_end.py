"""The whole port, driven with real DTOs against a panel of each line.

The panel client tests check routes in isolation. This drives RemnawaveImpl the
way the use cases do -- a UserDto and a plan or subscription in, a request on the
wire out -- so that a mistake in the request builders, in the identity decoding,
or in the dispatch between lines shows up here rather than against a live panel.
"""

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

import httpx
import pytest
from conftest import PANEL_ID, PANEL_UUID, SQUAD_UUID, PanelStub, user_payload
from remnapy.exceptions import NotFoundError

from src.application.dto import PlanSnapshotDto, SubscriptionDto, UserDto
from src.core.enums import PlanType, SubscriptionStatus
from src.core.utils.remna_id import encode_remna_id, remna_key
from src.infrastructure.services.remnawave import RemnawaveImpl

TELEGRAM_ID = 123456789
STORED_KEY = encode_remna_id(PANEL_ID)
EXPIRE_AT = datetime(2026, 12, 31, tzinfo=timezone.utc)


def metadata(version: str) -> Dict[str, Any]:
    return {
        "version": version,
        "build": {"time": "2026-01-01T00:00:00Z", "number": "1"},
        "git": {
            "backend": {
                "commitSha": "abc1234",
                "branch": "main",
                "commitUrl": "https://example.invalid/a",
            },
            "frontend": {"commitSha": "def5678", "commitUrl": "https://example.invalid/f"},
        },
    }


def a_user() -> UserDto:
    return UserDto(id=7, telegram_id=TELEGRAM_ID, name="Test", email="user@example.com")


def a_plan(**overrides: Any) -> PlanSnapshotDto:
    defaults: Dict[str, Any] = {
        "id": 1,
        "name": "Basic",
        "tag": "BASIC",
        "type": PlanType.BOTH,
        "traffic_limit": 100,
        "device_limit": 3,
        "duration": 30,
        "internal_squads": [UUID(SQUAD_UUID)],
        "external_squad": None,
    }
    defaults.update(overrides)
    return PlanSnapshotDto(**defaults)


def a_subscription(**overrides: Any) -> SubscriptionDto:
    defaults: Dict[str, Any] = {
        "user_remna_id": STORED_KEY,
        "status": SubscriptionStatus.ACTIVE,
        "traffic_limit": 100,
        "device_limit": 3,
        "traffic_limit_strategy": "NO_RESET",
        "expire_at": EXPIRE_AT,
        "url": f"https://sub.example.com/{'a' * 24}",
        "plan_snapshot": a_plan(),
        "internal_squads": [UUID(SQUAD_UUID)],
        "external_squad": None,
    }
    defaults.update(overrides)
    return SubscriptionDto(**defaults)


@pytest.fixture
async def v2_service(sdk: object, http: httpx.AsyncClient, panel: PanelStub) -> RemnawaveImpl:
    panel.on("GET", "/api/system/metadata", metadata("2.7.4"))
    panel.on("GET", f"/api/users/by-id/{PANEL_ID}", user_payload(line="v2"))
    service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
    await service.try_connection()
    return service


@pytest.fixture
async def v3_service(sdk: object, http: httpx.AsyncClient, panel: PanelStub) -> RemnawaveImpl:
    panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
    panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3"))
    service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
    await service.try_connection()
    return service


class TestCreatingAUser:
    """Creation is one code path: the route and body never diverged."""

    @pytest.mark.parametrize("version, line", [("2.7.4", "v2"), ("3.4.2", "v3")])
    async def test_a_plan_becomes_a_create_request(
        self,
        sdk: object,
        http: httpx.AsyncClient,
        panel: PanelStub,
        version: str,
        line: str,
    ) -> None:
        panel.on("GET", "/api/system/metadata", metadata(version))
        panel.on("POST", "/api/users", user_payload(line=line))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        created = await service.create_user(a_user(), plan=a_plan())

        body = panel.last.body
        assert panel.last.path == "/api/users"
        assert body["username"] == f"rs_{TELEGRAM_ID}"
        assert body["telegramId"] == TELEGRAM_ID
        assert body["trafficLimitBytes"] == 100 * 1024**3
        assert body["hwidDeviceLimit"] == 3
        assert body["activeInternalSquads"] == [SQUAD_UUID]
        assert body["tag"] == "BASIC"
        assert created.id == PANEL_ID

    async def test_no_uuid_is_ever_sent_on_creation(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # 3.x removed the field; sending it would be rejected there, and on 2.x
        # it would pin an identity RemnaShop no longer stores.
        panel.on("GET", "/api/system/metadata", metadata("2.7.4"))
        panel.on("POST", "/api/users", user_payload(line="v2"))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        await service.create_user(a_user(), plan=a_plan())

        assert "uuid" not in panel.last.body

    async def test_the_stored_key_comes_from_the_created_user(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # This is what a caller persists into user_remna_id.
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        panel.on("POST", "/api/users", user_payload(line="v3"))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        created = await service.create_user(a_user(), plan=a_plan())

        assert remna_key(created) == STORED_KEY


class TestUpdatingAUser:
    async def test_v2_updates_are_addressed_by_uuid(
        self, v2_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v2"))

        await v2_service.update_user(a_user(), STORED_KEY, subscription=a_subscription())

        assert panel.last.body["uuid"] == PANEL_UUID
        assert "id" not in panel.last.body

    async def test_v3_updates_are_addressed_by_id(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3_service.update_user(a_user(), STORED_KEY, subscription=a_subscription())

        assert panel.last.body["id"] == PANEL_ID
        assert "uuid" not in panel.last.body

    async def test_a_subscription_carries_its_limits_across(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3_service.update_user(
            a_user(),
            STORED_KEY,
            subscription=a_subscription(traffic_limit=50, device_limit=5),
        )

        assert panel.last.body["trafficLimitBytes"] == 50 * 1024**3
        assert panel.last.body["hwidDeviceLimit"] == 5

    async def test_a_disabled_subscription_is_sent_as_disabled(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3_service.update_user(
            a_user(),
            STORED_KEY,
            subscription=a_subscription(status=SubscriptionStatus.DISABLED),
        )

        assert panel.last.body["status"] == "DISABLED"

    async def test_an_expired_subscription_is_reactivated_not_left_expired(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        # Only DISABLED maps through; every other local status means the user
        # should be running, which is what a renewal relies on.
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3_service.update_user(
            a_user(),
            STORED_KEY,
            subscription=a_subscription(status=SubscriptionStatus.EXPIRED),
        )

        assert panel.last.body["status"] == "ACTIVE"

    async def test_moving_to_a_plan_without_an_external_squad_detaches(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3_service.update_user(a_user(), STORED_KEY, plan=a_plan(external_squad=None))

        assert panel.last.body["externalSquadUuid"] is None

    async def test_moving_to_a_plan_without_a_tag_clears_it(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3_service.update_user(a_user(), STORED_KEY, plan=a_plan(tag=None))

        assert panel.last.body["tag"] is None

    async def test_a_reset_follows_the_update_when_asked(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))
        panel.on("POST", f"/api/users/{PANEL_ID}/actions/reset-traffic", user_payload(line="v3"))

        await v3_service.update_user(
            a_user(), STORED_KEY, plan=a_plan(), reset_traffic=True
        )

        assert panel.last.path == f"/api/users/{PANEL_ID}/actions/reset-traffic"

    async def test_an_update_without_a_plan_or_subscription_is_refused(
        self, v3_service: RemnawaveImpl
    ) -> None:
        with pytest.raises(ValueError):
            await v3_service.update_user(a_user(), STORED_KEY)


class TestLifecycleActions:
    @pytest.mark.parametrize(
        "action, method",
        [("enable", "enable_user"), ("disable", "disable_user"), ("revoke", "revoke_subscription")],
    )
    async def test_v3_actions_reach_the_id_route(
        self, v3_service: RemnawaveImpl, panel: PanelStub, action: str, method: str
    ) -> None:
        panel.on("POST", f"/api/users/{PANEL_ID}/actions/{action}", user_payload(line="v3"))

        await getattr(v3_service, method)(STORED_KEY)

        assert panel.last.path == f"/api/users/{PANEL_ID}/actions/{action}"

    @pytest.mark.parametrize(
        "action, method",
        [("enable", "enable_user"), ("disable", "disable_user"), ("revoke", "revoke_subscription")],
    )
    async def test_v2_actions_reach_the_uuid_route(
        self, v2_service: RemnawaveImpl, panel: PanelStub, action: str, method: str
    ) -> None:
        panel.on("POST", f"/api/users/{PANEL_UUID}/actions/{action}", user_payload(line="v2"))

        await getattr(v2_service, method)(STORED_KEY)

        assert panel.last.path == f"/api/users/{PANEL_UUID}/actions/{action}"

    async def test_deletion_reports_success_on_both_lines(
        self, v2_service: RemnawaveImpl, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("DELETE", f"/api/users/{PANEL_UUID}", {"isDeleted": True})
        panel.on("DELETE", f"/api/users/{PANEL_ID}", None)

        assert await v2_service.delete_user(STORED_KEY) is True
        assert await v3_service.delete_user(STORED_KEY) is True


class TestReads:
    async def test_a_user_is_read_back_by_the_stored_key(
        self, v2_service: RemnawaveImpl, v3_service: RemnawaveImpl
    ) -> None:
        for service in (v2_service, v3_service):
            user = await service.get_user_by_uuid(STORED_KEY)
            assert user is not None and user.id == PANEL_ID

    async def test_listing_users_is_the_same_call_on_both_lines(
        self, v2_service: RemnawaveImpl, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/users", {"users": [user_payload(line="v3")], "total": 1})

        for service in (v2_service, v3_service):
            users = await service.get_all_users(limit=50, offset=100)
            assert [u.id for u in users] == [PANEL_ID]
            assert panel.last.query == {"start": "100", "size": "50"}

    async def test_squads_are_read_the_same_way_on_both_lines(
        self, v2_service: RemnawaveImpl, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        # Shaped as the panel really answers; the field set is identical in
        # both lines apart from `tags`, which 3.4.x added.
        panel.on(
            "GET",
            "/api/internal-squads",
            {
                "total": 1,
                "internalSquads": [
                    {
                        "uuid": SQUAD_UUID,
                        "name": "Main",
                        "viewPosition": 1,
                        "info": {"membersCount": 5, "inboundsCount": 2},
                        "inbounds": [],
                        "createdAt": "2025-01-01T00:00:00.000Z",
                        "updatedAt": "2025-06-01T00:00:00.000Z",
                    }
                ],
            },
        )

        for service in (v2_service, v3_service):
            squads = await service.get_internal_squads()
            assert [s.name for s in squads] == ["Main"]


class TestUnresolvableIdentity:
    async def test_an_action_on_an_unresolvable_key_raises_rather_than_acting(
        self, v3_service: RemnawaveImpl, panel: PanelStub
    ) -> None:
        # A legacy uuid on a 3.x panel that matches nothing: acting on a guessed
        # id here would touch a stranger's account.
        orphan = UUID("6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c")
        before = len(panel.calls)

        with pytest.raises(NotFoundError):
            await v3_service.enable_user(orphan)

        acted = [c for c in panel.calls[before:] if "/actions/" in c.path]
        assert acted == []

    async def test_a_read_on_an_unresolvable_key_is_simply_empty(
        self, v3_service: RemnawaveImpl
    ) -> None:
        orphan = UUID("6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c")

        assert await v3_service.get_user_by_uuid(orphan) is None
        assert await v3_service.get_devices(orphan) == []
