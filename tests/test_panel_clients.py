"""Both panel clients, checked against the routes each panel line really exposes.

These are contract tests: they assert the exact method, path and body that go on
the wire, because that is where the two lines diverge and where a silent mistake
would show up only against a live panel.
"""

from uuid import UUID

import httpx
import pytest
from conftest import (
    PANEL_ID,
    PANEL_UUID,
    SQUAD_UUID,
    TELEGRAM_ID,
    PanelStub,
    device_payload,
    user_payload,
)
from remnapy.exceptions import NotFoundError
from remnapy.models import UpdateUserRequestDto

from src.core.utils.remna_id import encode_remna_id
from src.infrastructure.services.remnawave import RemnawaveImpl
from src.infrastructure.services.remnawave._v2 import PanelClientV2
from src.infrastructure.services.remnawave._v3 import PanelClientV3


@pytest.fixture
def v2(http: httpx.AsyncClient) -> PanelClientV2:
    return PanelClientV2(http)


@pytest.fixture
def v3(sdk: object, http: httpx.AsyncClient) -> PanelClientV3:
    return PanelClientV3(sdk, http)  # type: ignore[arg-type]


def stub_v2_user(panel: PanelStub) -> None:
    panel.on("GET", f"/api/users/by-id/{PANEL_ID}", user_payload(line="v2"))


class TestUserLookupRoutes:
    async def test_v2_reads_a_user_through_by_id(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)

        user = await v2.get_user(PANEL_ID)

        assert user is not None and user.id == PANEL_ID
        assert panel.last.path == f"/api/users/by-id/{PANEL_ID}"

    async def test_v3_reads_a_user_from_the_bare_id_route(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3"))

        user = await v3.get_user(PANEL_ID)

        assert user is not None and user.id == PANEL_ID
        assert panel.last.path == f"/api/users/{PANEL_ID}"

    async def test_a_missing_user_is_none_on_both_lines(
        self, v2: PanelClientV2, v3: PanelClientV3
    ) -> None:
        # Nothing is stubbed, so the panel answers 404.
        assert await v2.get_user(PANEL_ID) is None
        assert await v3.get_user(PANEL_ID) is None


class TestIdentityTranslation:
    async def test_v2_resolves_the_uuid_actions_still_need(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)

        assert str(await v2.get_user_uuid(PANEL_ID)) == PANEL_UUID

    async def test_v3_has_no_uuid_to_offer(self, v3: PanelClientV3) -> None:
        # The column does not exist on a 3.x panel; claiming otherwise would
        # invite callers to persist a fabricated identifier.
        assert await v3.get_user_uuid(PANEL_ID) is None

    async def test_v2_resolves_once_and_reuses_it(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("POST", f"/api/users/{PANEL_UUID}/actions/enable", user_payload(line="v2"))
        panel.on("POST", f"/api/users/{PANEL_UUID}/actions/disable", user_payload(line="v2"))

        await v2.enable_user(PANEL_ID)
        await v2.disable_user(PANEL_ID)

        lookups = [c for c in panel.calls if c.path.endswith(f"by-id/{PANEL_ID}")]
        assert len(lookups) == 1, "the id -> uuid mapping is immutable, resolve it once"

    async def test_v2_action_on_a_missing_user_raises_not_found(
        self, v2: PanelClientV2
    ) -> None:
        with pytest.raises(NotFoundError):
            await v2.enable_user(PANEL_ID)


class TestActionRoutes:
    async def test_v2_actions_address_the_user_by_uuid(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        for action, method in [
            ("enable", "POST"),
            ("disable", "POST"),
            ("reset-traffic", "POST"),
            ("revoke", "POST"),
        ]:
            panel.on(method, f"/api/users/{PANEL_UUID}/actions/{action}", user_payload(line="v2"))

        await v2.enable_user(PANEL_ID)
        await v2.disable_user(PANEL_ID)
        await v2.reset_traffic(PANEL_ID)
        await v2.revoke_subscription(PANEL_ID)

        acted = [c.path for c in panel.calls if "/actions/" in c.path]
        assert acted == [
            f"/api/users/{PANEL_UUID}/actions/enable",
            f"/api/users/{PANEL_UUID}/actions/disable",
            f"/api/users/{PANEL_UUID}/actions/reset-traffic",
            f"/api/users/{PANEL_UUID}/actions/revoke",
        ]

    async def test_v3_actions_address_the_user_by_id(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        for action in ["enable", "disable", "reset-traffic", "revoke"]:
            panel.on("POST", f"/api/users/{PANEL_ID}/actions/{action}", user_payload(line="v3"))

        await v3.enable_user(PANEL_ID)
        await v3.disable_user(PANEL_ID)
        await v3.reset_traffic(PANEL_ID)
        await v3.revoke_subscription(PANEL_ID)

        acted = [c.path for c in panel.calls if "/actions/" in c.path]
        assert acted == [
            f"/api/users/{PANEL_ID}/actions/enable",
            f"/api/users/{PANEL_ID}/actions/disable",
            f"/api/users/{PANEL_ID}/actions/reset-traffic",
            f"/api/users/{PANEL_ID}/actions/revoke",
        ]


class TestDeletion:
    async def test_v2_deletes_by_uuid_and_reports_the_flag(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("DELETE", f"/api/users/{PANEL_UUID}", {"isDeleted": True})

        assert await v2.delete_user(PANEL_ID) is True
        assert panel.last.path == f"/api/users/{PANEL_UUID}"

    async def test_v2_forgets_the_uuid_after_deleting(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        # A stale entry would let a later id reuse resolve to a dead uuid.
        stub_v2_user(panel)
        panel.on("DELETE", f"/api/users/{PANEL_UUID}", {"isDeleted": True})

        await v2.delete_user(PANEL_ID)
        await v2.get_user_uuid(PANEL_ID)

        lookups = [c for c in panel.calls if c.path.endswith(f"by-id/{PANEL_ID}")]
        assert len(lookups) == 2

    async def test_v3_deletes_by_id(self, v3: PanelClientV3, panel: PanelStub) -> None:
        # 3.x answers a deletion with no body: the isDeleted flag 2.x returned
        # was dropped from the contract, so success is "no error raised".
        panel.on("DELETE", f"/api/users/{PANEL_ID}", None)

        assert await v3.delete_user(PANEL_ID) is True
        assert panel.last.path == f"/api/users/{PANEL_ID}"

    async def test_deleting_a_missing_user_is_false_not_an_error(
        self, v2: PanelClientV2, v3: PanelClientV3
    ) -> None:
        assert await v2.delete_user(PANEL_ID) is False
        assert await v3.delete_user(PANEL_ID) is False


class TestLookupByTelegramId:
    async def test_v2_uses_the_dedicated_route(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        panel.on("GET", f"/api/users/by-telegram-id/{TELEGRAM_ID}", [user_payload(line="v2")])

        users = await v2.find_users_by_telegram_id(TELEGRAM_ID)

        assert [u.id for u in users] == [PANEL_ID]
        assert panel.last.path == f"/api/users/by-telegram-id/{TELEGRAM_ID}"

    async def test_v3_falls_back_to_a_list_filter(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # 3.0.0 deleted by-telegram-id; the list filter is the replacement.
        panel.on("GET", "/api/users", {"users": [user_payload(line="v3")], "total": 1})

        users = await v3.find_users_by_telegram_id(TELEGRAM_ID)

        assert [u.id for u in users] == [PANEL_ID]
        assert panel.last.path == "/api/users"
        assert "telegramId" in panel.last.query["filters"]

    async def test_v3_discards_substring_matches_from_the_panel(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # The panel filters telegramId with LIKE '%value%' over a text cast, so
        # searching 123456789 also returns 9123456789. Handing that back would
        # attach one person's subscription to another's account.
        impostor = user_payload(line="v3", panel_id=999, telegramId=9123456789)
        panel.on(
            "GET",
            "/api/users",
            {"users": [impostor, user_payload(line="v3")], "total": 2},
        )

        users = await v3.find_users_by_telegram_id(TELEGRAM_ID)

        assert [u.id for u in users] == [PANEL_ID]

    async def test_v3_returns_nothing_when_only_substring_matches_come_back(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        impostor = user_payload(line="v3", panel_id=999, telegramId=9123456789)
        panel.on("GET", "/api/users", {"users": [impostor], "total": 1})

        assert await v3.find_users_by_telegram_id(TELEGRAM_ID) == []


class TestLookupByEmail:
    async def test_v2_uses_the_dedicated_route(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/users/by-email/user@example.com", [user_payload(line="v2")])

        users = await v2.find_users_by_email("user@example.com")

        assert [u.id for u in users] == [PANEL_ID]

    async def test_v3_discards_substring_matches(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        impostor = user_payload(line="v3", panel_id=999, email="not-user@example.com")
        panel.on(
            "GET",
            "/api/users",
            {"users": [impostor, user_payload(line="v3")], "total": 2},
        )

        users = await v3.find_users_by_email("user@example.com")

        assert [u.id for u in users] == [PANEL_ID]

    async def test_v3_matches_email_case_insensitively(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/users", {"users": [user_payload(line="v3")], "total": 1})

        users = await v3.find_users_by_email("USER@Example.COM")

        assert [u.id for u in users] == [PANEL_ID]

    async def test_v3_tolerates_a_user_without_an_email(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        nobody = user_payload(line="v3", panel_id=999, email=None)
        panel.on("GET", "/api/users", {"users": [nobody], "total": 1})

        assert await v3.find_users_by_email("user@example.com") == []


class TestDevices:
    async def test_v2_lists_devices_by_uuid_and_fills_the_owner_id(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on(
            "GET",
            f"/api/hwid/devices/{PANEL_UUID}",
            {"total": 1, "devices": [device_payload(line="v2")]},
        )

        devices = await v2.get_devices(PANEL_ID)

        assert [d.hwid for d in devices] == ["HW-1"]
        # 2.x sends userUuid, the model wants the numeric owner.
        assert devices[0].user_id == PANEL_ID

    async def test_v3_lists_devices_by_id(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on(
            "GET",
            f"/api/hwid/devices/{PANEL_ID}",
            {"total": 1, "devices": [device_payload(line="v3")]},
        )

        devices = await v3.get_devices(PANEL_ID)

        assert [d.hwid for d in devices] == ["HW-1"]
        assert panel.last.path == f"/api/hwid/devices/{PANEL_ID}"

    async def test_no_devices_is_an_empty_list_on_both_lines(
        self, v2: PanelClientV2, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("GET", f"/api/hwid/devices/{PANEL_UUID}", {"total": 0, "devices": []})
        panel.on("GET", f"/api/hwid/devices/{PANEL_ID}", {"total": 0, "devices": []})

        assert await v2.get_devices(PANEL_ID) == []
        assert await v3.get_devices(PANEL_ID) == []

    async def test_v2_deletes_a_device_with_the_uuid_body(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("POST", "/api/hwid/devices/delete", {"total": 0, "devices": []})

        assert await v2.delete_device(PANEL_ID, "HW-1") == 0
        assert panel.last.body == {"userUuid": PANEL_UUID, "hwid": "HW-1"}

    async def test_v3_deletes_a_device_with_the_id_body(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on("POST", "/api/hwid/devices/delete", {"total": 0, "devices": []})

        assert await v3.delete_device(PANEL_ID, "HW-1") == 0
        assert panel.last.body == {"userId": PANEL_ID, "hwid": "HW-1"}

    async def test_v2_deletes_all_devices_with_the_uuid_body(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("POST", "/api/hwid/devices/delete-all", {"total": 0, "devices": []})

        await v2.delete_all_devices(PANEL_ID)

        assert panel.last.body == {"userUuid": PANEL_UUID}

    async def test_v3_deletes_all_devices_with_the_id_body(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on("POST", "/api/hwid/devices/delete-all", {"total": 0, "devices": []})

        await v3.delete_all_devices(PANEL_ID)

        assert panel.last.body == {"userId": PANEL_ID}


class TestDropConnections:
    async def test_v2_posts_to_ip_control_with_user_uuids(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("POST", "/api/ip-control/drop-connections", {"eventSent": True})

        await v2.drop_connections(PANEL_ID)

        assert panel.last.path == "/api/ip-control/drop-connections"
        assert panel.last.body == {
            "dropBy": {"by": "userUuids", "userUuids": [PANEL_UUID]},
            "targetNodes": {"target": "allNodes"},
        }

    async def test_v3_posts_to_connections_with_user_ids(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # 3.0.0 renamed the controller and re-keyed the body onto ids.
        panel.on("POST", "/api/connections/drop", {"eventSent": True})

        await v3.drop_connections(PANEL_ID)

        assert panel.last.path == "/api/connections/drop"
        assert panel.last.body == {
            "dropBy": {"by": "userIds", "userIds": [PANEL_ID]},
            "targetNodes": {"target": "allNodes"},
        }


class TestUpdate:
    async def test_v2_addresses_the_update_by_uuid(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("PATCH", "/api/users", user_payload(line="v2"))

        await v2.update_user(PANEL_ID, UpdateUserRequestDto(traffic_limit_bytes=1024))

        assert panel.last.path == "/api/users"
        assert panel.last.body == {"uuid": PANEL_UUID, "trafficLimitBytes": 1024}

    async def test_v3_addresses_the_update_by_id(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3.update_user(PANEL_ID, UpdateUserRequestDto(traffic_limit_bytes=1024))

        assert panel.last.path == "/api/users"
        assert panel.last.body == {"id": PANEL_ID, "trafficLimitBytes": 1024}

    async def test_the_username_is_never_used_to_address_an_update(
        self, v2: PanelClientV2, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # An imported user's panel username is whatever it was before RemnaShop
        # adopted it, so addressing by name would update the wrong account.
        stub_v2_user(panel)
        panel.on("PATCH", "/api/users", user_payload(line="v2"))
        request = UpdateUserRequestDto(username="rs_123456789", traffic_limit_bytes=1)

        await v2.update_user(PANEL_ID, request)
        assert "username" not in panel.last.body

        await v3.update_user(PANEL_ID, UpdateUserRequestDto(username="x", traffic_limit_bytes=1))
        assert "username" not in panel.last.body


class TestClearingATag:
    async def test_an_explicit_null_tag_reaches_the_panel(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # Moving a user from a tagged plan onto an untagged one must clear the
        # tag; leaving it would show the old plan's label in the panel forever.
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3.update_user(
            PANEL_ID, UpdateUserRequestDto(traffic_limit_bytes=1, tag=None)
        )

        assert panel.last.body["tag"] is None

    async def test_a_tag_that_was_never_set_stays_absent(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3.update_user(PANEL_ID, UpdateUserRequestDto(traffic_limit_bytes=1))

        assert "tag" not in panel.last.body

    async def test_setting_a_tag_still_sends_it(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # An imported subscription carries IMPORTED in its own plan snapshot and
        # keeps re-sending it, which is what preserves the marker.
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3.update_user(PANEL_ID, UpdateUserRequestDto(tag="IMPORTED"))

        assert panel.last.body["tag"] == "IMPORTED"

    async def test_the_2_x_line_clears_a_tag_too(
        self, v2: PanelClientV2, panel: PanelStub
    ) -> None:
        stub_v2_user(panel)
        panel.on("PATCH", "/api/users", user_payload(line="v2"))

        await v2.update_user(
            PANEL_ID, UpdateUserRequestDto(traffic_limit_bytes=1, tag=None)
        )

        assert panel.last.body["tag"] is None


class TestDetachingAnExternalSquad:
    async def test_an_explicit_null_reaches_the_panel_on_both_lines(
        self, v2: PanelClientV2, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # Moving a user onto a plan with no external squad must detach them.
        # Omitting the field would leave them in the old squad.
        stub_v2_user(panel)
        panel.on("PATCH", "/api/users", user_payload(line="v2"))
        request = UpdateUserRequestDto(traffic_limit_bytes=1, external_squad_uuid=None)

        await v2.update_user(PANEL_ID, request)
        assert panel.last.body["externalSquadUuid"] is None

        await v3.update_user(
            PANEL_ID,
            UpdateUserRequestDto(traffic_limit_bytes=1, external_squad_uuid=None),
        )
        assert panel.last.body["externalSquadUuid"] is None

    async def test_a_field_that_was_never_set_stays_absent(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # "Leave it alone" and "clear it" have to stay distinguishable.
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3.update_user(PANEL_ID, UpdateUserRequestDto(traffic_limit_bytes=1))

        assert "externalSquadUuid" not in panel.last.body

    async def test_setting_a_squad_still_sends_the_uuid(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3.update_user(
            PANEL_ID,
            UpdateUserRequestDto(external_squad_uuid=UUID(SQUAD_UUID)),
        )

        assert panel.last.body["externalSquadUuid"] == SQUAD_UUID

    async def test_fields_the_panel_rejects_as_null_are_never_sent_as_null(
        self, v3: PanelClientV3, panel: PanelStub
    ) -> None:
        # expireAt, status, trafficLimit* and activeInternalSquads are optional
        # but not nullable in either line: a null there fails validation.
        panel.on("PATCH", "/api/users", user_payload(line="v3"))

        await v3.update_user(
            PANEL_ID,
            UpdateUserRequestDto(
                traffic_limit_bytes=None,
                expire_at=None,
                status=None,
                active_internal_squads=None,
                traffic_limit_strategy=None,
            ),
        )

        assert panel.last.body == {"id": PANEL_ID}


class TestContractDetection:
    """Which contract the service speaks, and how it settles on one."""

    def build(self, sdk: object, http: httpx.AsyncClient) -> RemnawaveImpl:
        return RemnawaveImpl(sdk, http)  # type: ignore[arg-type]

    def metadata(self, version: str) -> dict:
        # Shaped as GET /system/metadata really answers; only version matters here.
        return {
            "version": version,
            "build": {"time": "2026-01-01T00:00:00Z", "number": "1"},
            "git": {
                "backend": {
                    "commitSha": "abc1234",
                    "branch": "main",
                    "commitUrl": "https://example.invalid/abc1234",
                },
                "frontend": {
                    "commitSha": "def5678",
                    "commitUrl": "https://example.invalid/def5678",
                },
            },
        }

    async def test_a_three_x_panel_is_addressed_by_id(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", self.metadata("3.4.2"))
        panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3"))
        service = self.build(sdk, http)

        await service.try_connection()
        await service.get_user_by_uuid(encode_remna_id(PANEL_ID))

        assert panel.last.path == f"/api/users/{PANEL_ID}"

    async def test_a_two_x_panel_is_addressed_by_uuid(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", self.metadata("2.7.4"))
        panel.on("GET", f"/api/users/by-id/{PANEL_ID}", user_payload(line="v2"))
        service = self.build(sdk, http)

        await service.try_connection()
        await service.get_user_by_uuid(encode_remna_id(PANEL_ID))

        assert panel.last.path == f"/api/users/by-id/{PANEL_ID}"

    async def test_a_panel_below_the_floor_is_refused(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", self.metadata("2.6.4"))

        with pytest.raises(ValueError):
            await self.build(sdk, http).try_connection()

    async def test_the_contract_is_settled_on_first_use_if_startup_missed_it(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # The panel is often still starting when the bot is, especially right
        # after a panel upgrade. Without this the bot would keep speaking the
        # older contract until somebody restarted it.
        panel.on("GET", "/api/system/metadata", self.metadata("3.4.2"))
        panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3"))
        service = self.build(sdk, http)

        # try_connection was never called, as if the panel had been unreachable.
        await service.get_user_by_uuid(encode_remna_id(PANEL_ID))

        assert panel.last.path == f"/api/users/{PANEL_ID}"

    async def test_the_contract_is_settled_only_once(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        panel.on("GET", "/api/system/metadata", self.metadata("3.4.2"))
        panel.on("GET", f"/api/users/{PANEL_ID}", user_payload(line="v3"))
        service = self.build(sdk, http)

        for _ in range(3):
            await service.get_user_by_uuid(encode_remna_id(PANEL_ID))

        handshakes = [c for c in panel.calls if c.path.endswith("/system/metadata")]
        assert len(handshakes) == 1

    async def test_an_unreachable_panel_does_not_break_the_caller(
        self, sdk: object, http: httpx.AsyncClient, panel: PanelStub
    ) -> None:
        # Nothing is stubbed: metadata 404s, so the line stays unknown. The call
        # should report "no such user", not blow up on the handshake.
        service = self.build(sdk, http)

        assert await service.get_user_by_uuid(encode_remna_id(PANEL_ID)) is None
