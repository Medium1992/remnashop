"""Creating a user whose username the panel already has.

An older RemnaShop talking to a 3.x panel creates the panel user on every
purchase or trial and then fails to parse the reply, which lacks the uuid the
old SDK insists on. Nothing is saved on the RemnaShop side, so the panel is left
holding ``rs_<telegram id>`` with no subscription pointing at it. After moving
to this build, the next attempt hits "username already exists" -- and without
handling it, that account could never get a subscription again.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import PANEL_ID, PANEL_UUID, PanelStub, user_payload
from remnapy.exceptions import ApiError
from test_service_end_to_end import a_plan, a_user, metadata

from src.core.utils.remna_id import encode_remna_id, remna_key
from src.infrastructure.services.remnawave import RemnawaveImpl

TAKEN = {"message": "User username already exists", "code": "A019", "statusCode": 400}
USERNAME = "rs_123456789"


async def connected(
    sdk: object, http: httpx.AsyncClient, panel: PanelStub, line: str
) -> RemnawaveImpl:
    panel.on("GET", "/api/system/metadata", metadata("2.7.4" if line == "v2" else "3.4.5"))
    service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
    await service.try_connection()
    return service


def stub_orphan(panel: PanelStub, line: str, **overrides: object) -> None:
    orphan = user_payload(line=line, **overrides)
    panel.on("POST", "/api/users", TAKEN, status=400)
    panel.on("GET", f"/api/users/by-username/{USERNAME}", orphan)
    panel.on("PATCH", "/api/users", orphan)
    if line == "v2":
        panel.on("GET", f"/api/users/by-id/{PANEL_ID}", orphan)


class TestAdoption:
    @pytest.mark.parametrize("line", ["v2", "v3"])
    async def test_the_existing_user_is_taken_over_not_duplicated(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub, line: str
    ) -> None:
        service = await connected(sdk, http, panel, line)
        stub_orphan(panel, line)

        created = await service.create_user(a_user(), plan=a_plan())

        assert created.id == PANEL_ID
        patch = [c for c in panel.calls if c.method == "PATCH"][-1]
        key = "uuid" if line == "v2" else "id"
        assert patch.body[key] == (PANEL_UUID if line == "v2" else PANEL_ID)

    async def test_the_adopted_user_yields_the_key_the_caller_stores(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        service = await connected(sdk, http, panel, "v3")
        stub_orphan(panel, "v3")

        created = await service.create_user(a_user(), plan=a_plan())

        assert remna_key(created) == encode_remna_id(PANEL_ID)

    async def test_the_plan_is_applied_to_the_adopted_user(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        service = await connected(sdk, http, panel, "v3")
        stub_orphan(panel, "v3")

        await service.create_user(a_user(), plan=a_plan(traffic_limit=50, device_limit=5))

        body = panel.last.body
        assert body["trafficLimitBytes"] == 50 * 1024**3
        assert body["hwidDeviceLimit"] == 5
        assert body["status"] == "ACTIVE"


class TestExpiry:
    async def test_a_longer_expiry_on_the_orphan_is_kept(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # The orphan may carry time that was paid for; a 30-day plan must not
        # cut it short.
        far = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat()
        service = await connected(sdk, http, panel, "v3")
        stub_orphan(panel, "v3", expireAt=far)

        await service.create_user(a_user(), plan=a_plan(duration=30))

        sent = datetime.fromisoformat(panel.last.body["expireAt"].replace("Z", "+00:00"))
        assert sent >= datetime.now(timezone.utc) + timedelta(days=199)

    async def test_a_shorter_expiry_on_the_orphan_is_extended(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        near = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        service = await connected(sdk, http, panel, "v3")
        stub_orphan(panel, "v3", expireAt=near)

        await service.create_user(a_user(), plan=a_plan(duration=30))

        sent = datetime.fromisoformat(panel.last.body["expireAt"].replace("Z", "+00:00"))
        assert sent >= datetime.now(timezone.utc) + timedelta(days=29)


class TestOtherFailures:
    async def test_a_different_rejection_still_raises(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # Only "username taken" is safe to adopt from; anything else is a real
        # failure the caller has to see.
        service = await connected(sdk, http, panel, "v3")
        panel.on(
            "POST",
            "/api/users",
            {"message": "boom", "code": "A001", "statusCode": 500},
            status=500,
        )

        with pytest.raises(ApiError):
            await service.create_user(a_user(), plan=a_plan())

        assert not [c for c in panel.calls if c.method == "PATCH"]

    async def test_a_normal_creation_does_not_look_anything_up(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        service = await connected(sdk, http, panel, "v3")
        panel.on("POST", "/api/users", user_payload(line="v3"))

        await service.create_user(a_user(), plan=a_plan())

        assert not [c for c in panel.calls if "by-username" in c.path]
