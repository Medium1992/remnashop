"""Edges that only show up against a panel that is slow, angry, or old.

Everything here is a path a live deployment will eventually take: the panel
under load, a proxy rewriting a response, a burst of requests at startup, a user
imported from XUI. None of them are exercised by the happy-path tests.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

import httpx
import orjson
import pytest
from conftest import PANEL_ID, PANEL_UUID, PanelStub, user_payload
from remnapy.exceptions import ApiError, ServerError
from remnapy.models import CreateUserRequestDto
from test_service_end_to_end import metadata

from src.application.use_cases.importer.dto import ExportedUserDto
from src.core.enums import SubscriptionStatus
from src.core.utils.remna_id import encode_remna_id
from src.infrastructure.services.remnawave import RemnawaveImpl
from src.infrastructure.services.remnawave._v2 import PanelClientV2

STORED_KEY = encode_remna_id(PANEL_ID)


class TestConcurrentStartup:
    async def test_a_burst_of_calls_triggers_one_handshake(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # The port is a singleton. Without guarding, every request that arrives
        # before the panel answered would fire its own metadata call.
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3"))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]

        await asyncio.gather(*(service.get_user_by_uuid(STORED_KEY) for _ in range(20)))

        handshakes = [c for c in panel.calls if c.path.endswith("/system/metadata")]
        assert len(handshakes) == 1

    async def test_concurrent_calls_all_use_the_right_contract(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3"))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]

        results = await asyncio.gather(
            *(service.get_user_by_uuid(STORED_KEY) for _ in range(10))
        )

        assert all(r is not None and r.id == PANEL_ID for r in results)
        # None of them fell through to the 2.x route.
        assert not [c for c in panel.calls if "by-id" in c.path]


class TestPanelErrors:
    async def test_a_server_error_is_raised_not_swallowed(
        self, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # Treating a 500 as "no such user" would let a renewal quietly create a
        # duplicate rather than retrying.
        panel.on(
            "GET",
            f"/api/users/by-id/{PANEL_ID}",
            {"message": "boom", "code": "A001", "statusCode": 500},
            status=500,
        )

        with pytest.raises(ApiError):
            await PanelClientV2(http).get_user(PANEL_ID)

    async def test_a_server_error_maps_to_the_same_type_the_sdk_uses(
        self, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # The 2.x client issues raw requests, so it has to raise what callers
        # already catch from the SDK.
        panel.on(
            "POST",
            "/api/hwid/devices/delete",
            {"message": "boom", "code": "A001", "statusCode": 500},
            status=500,
        )
        panel.on("GET", f"/api/users/by-id/{PANEL_ID}", user_payload(line="v2"))

        with pytest.raises(ServerError):
            await PanelClientV2(http).delete_device(PANEL_ID, "HW-1")

    async def test_a_non_json_error_body_still_raises_cleanly(
        self, panel: PanelStub
    ) -> None:
        # A proxy in front of the panel can answer HTML on a bad day.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="<html>bad gateway</html>")

        client = httpx.AsyncClient(
            base_url="http://panel.test/api", transport=httpx.MockTransport(handler)
        )

        with pytest.raises(ApiError):
            await PanelClientV2(client).get_user(PANEL_ID)

        await client.aclose()

    async def test_an_empty_body_on_an_action_is_not_an_error(
        self, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # Some panel actions answer 200 with no content at all.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(f"by-id/{PANEL_ID}"):
                return httpx.Response(200, json={"response": user_payload(line="v2")})
            return httpx.Response(200, content=b"")

        client = httpx.AsyncClient(
            base_url="http://panel.test/api", transport=httpx.MockTransport(handler)
        )

        await PanelClientV2(client).enable_user(PANEL_ID)

        await client.aclose()


class TestPagination:
    async def test_the_offset_and_limit_reach_the_panel_unchanged(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # The sync job walks every user through this; an off-by-one here would
        # skip or double-process accounts.
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        panel.on("GET", "/api/users", {"users": [], "total": 0})
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        for offset, limit in [(0, 100), (100, 100), (250, 50)]:
            await service.get_all_users(limit=limit, offset=offset)
            assert panel.last.query == {"start": str(offset), "size": str(limit)}

    async def test_an_empty_page_is_an_empty_list(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        panel.on("GET", "/api/users", {"users": [], "total": 0})
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        assert await service.get_all_users(limit=100, offset=0) == []


def through_taskiq(exported: ExportedUserDto) -> Dict[str, Any]:
    """What the import task actually receives.

    The exported records are handed to a taskiq task, so they cross an orjson
    round trip on the way and arrive as plain dicts, not dataclasses.
    """
    return orjson.loads(orjson.dumps(exported))


class TestImporterPayload:
    def test_an_exported_xui_user_becomes_a_valid_create_request(self) -> None:
        # The importer builds its request straight from the exported record;
        # dropping uuid from the model must not have broken that conversion.
        exported = ExportedUserDto(
            username="rs_123456789",
            telegram_id=123456789,
            status=SubscriptionStatus.ACTIVE,
            expire_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
            traffic_limit_bytes=107374182400,
            hwid_device_limit=3,
            tag="IMPORTED",
        )

        request = CreateUserRequestDto.model_validate(through_taskiq(exported))

        assert request.username == "rs_123456789"
        assert request.telegram_id == 123456789
        assert request.tag == "IMPORTED"
        assert request.hwid_device_limit == 3

    def test_the_imported_tag_survives_serialisation(self) -> None:
        # The tag is what lets RemnaShop adopt the user on the created webhook.
        exported = ExportedUserDto(
            username="rs_1",
            telegram_id=1,
            status=SubscriptionStatus.ACTIVE,
            expire_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
            traffic_limit_bytes=0,
            hwid_device_limit=0,
            tag="IMPORTED",
        )
        request = CreateUserRequestDto.model_validate(through_taskiq(exported))

        body: Dict[str, Any] = json.loads(
            request.model_dump_json(exclude_none=True, by_alias=True)
        )

        assert body["tag"] == "IMPORTED"
        assert "uuid" not in body


class TestLegacyRowsAtRuntime:
    async def test_a_legacy_row_is_resolved_through_its_short_uuid(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # A row the backfill could not reach, hit at runtime on a 3.x panel.
        short_uuid = user_payload(line="v3")["shortUuid"]
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        panel.on("GET", f"/api/users/by-short-uuid/{short_uuid}", user_payload(line="v3"))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        resolved = await service.resolve_legacy_identity(
            UUID(PANEL_UUID),
            subscription_url=f"https://sub.example.com/{short_uuid}",
        )

        assert resolved == PANEL_ID

    async def test_the_username_is_the_last_resort(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        panel.on("GET", "/api/users/by-username/rs_123456789", user_payload(line="v3"))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        resolved = await service.resolve_legacy_identity(
            UUID(PANEL_UUID),
            subscription_url="https://sub.example.com/",
            username="rs_123456789",
        )

        assert resolved == PANEL_ID

    async def test_nothing_matching_resolves_to_nothing(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", metadata("3.4.2"))
        service = RemnawaveImpl(sdk, http)  # type: ignore[arg-type]
        await service.try_connection()

        resolved = await service.resolve_legacy_identity(
            UUID(PANEL_UUID),
            subscription_url="https://sub.example.com/abcdef0123456789abcd",
            username="rs_nobody",
        )

        assert resolved is None
