"""What ends up in ``user_remna_id``, and the code that reads it back.

The stored key is the one piece of this work that touches production rows and
that a future migration has to interpret. So the properties worth pinning are:
every path that produces a key produces the same encoded form, on either panel
line, and the code that compares or copies keys keeps them consistent.
"""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

import pytest
from conftest import PANEL_ID, SHORT_UUID, SQUAD_UUID, user_payload
from remnapy.models import UserResponseDto
from remnapy.models.webhook import UserDto as WebhookUserDto
from test_service_end_to_end import a_plan

from src.application.dto import RemnaSubscriptionDto, SubscriptionDto
from src.core.enums import PlanType, SubscriptionStatus
from src.core.utils.converters import bytes_to_gb
from src.core.utils.remna_id import (
    SYNTHETIC_UPPER_BOUND,
    decode_remna_id,
    encode_remna_id,
    is_synthetic_remna_id,
    remna_key,
)
from src.infrastructure.services.remnawave import RemnawaveImpl
from src.infrastructure.services.remnawave._v2 import PanelClientV2


def parsed_user(line: str, **overrides: Any) -> Any:
    return UserResponseDto.model_validate(user_payload(line=line, **overrides))


def webhook_user(line: str, **overrides: Any) -> Any:
    return WebhookUserDto.model_validate(user_payload(line=line, **overrides))


class TestEveryKeyIsSynthetic:
    """No path may store a key a future migration cannot decode."""

    @pytest.mark.parametrize("line", ["v2", "v3"])
    def test_an_api_response_yields_a_synthetic_key(self, line: str) -> None:
        assert is_synthetic_remna_id(remna_key(parsed_user(line)))

    @pytest.mark.parametrize("line", ["v2", "v3"])
    def test_a_webhook_payload_yields_a_synthetic_key(self, line: str) -> None:
        assert is_synthetic_remna_id(remna_key(webhook_user(line)))

    @pytest.mark.parametrize("line", ["v2", "v3"])
    def test_a_remna_subscription_yields_a_synthetic_key(self, line: str) -> None:
        # This is what the sync path persists into user_remna_id.
        sub = RemnaSubscriptionDto.from_remna_user(parsed_user(line))
        assert is_synthetic_remna_id(sub.uuid)

    def test_the_same_user_yields_the_same_key_on_both_lines(self) -> None:
        # The point of the whole scheme: a panel upgrade must not re-key anyone.
        before = remna_key(parsed_user("v2"))
        after = remna_key(parsed_user("v3"))

        assert before == after == encode_remna_id(PANEL_ID)

    def test_the_two_x_panel_uuid_never_becomes_the_key(self) -> None:
        # It is right there in the payload and would be the obvious thing to
        # grab; storing it is exactly what stops working after an upgrade.
        payload = user_payload(line="v2")
        assert remna_key(parsed_user("v2")) != UUID(payload["uuid"])

    @pytest.mark.parametrize("panel_id", [1, 2, 12345, 2**31, 2**40])
    def test_keys_stay_decodable_across_the_plausible_id_range(self, panel_id: int) -> None:
        key = remna_key(parsed_user("v3", id=panel_id))

        assert decode_remna_id(key) == panel_id
        assert key < SYNTHETIC_UPPER_BOUND


class TestSubscriptionMatching:
    """A mismatch here triggers a resync, so a false negative is expensive."""

    def bot_subscription(self, remna: RemnaSubscriptionDto) -> SubscriptionDto:
        return SubscriptionDto(
            user_remna_id=remna.uuid,
            status=remna.status,
            traffic_limit=remna.traffic_limit,
            device_limit=remna.device_limit,
            traffic_limit_strategy=remna.traffic_limit_strategy,
            tag=remna.tag,
            internal_squads=list(remna.internal_squads),
            external_squad=remna.external_squad,
            expire_at=remna.expire_at,
            url=remna.url,
            plan_snapshot=a_plan(),
        )

    @pytest.mark.parametrize("line", ["v2", "v3"])
    def test_a_faithful_copy_matches_on_either_line(self, line: str) -> None:
        remna = RemnaSubscriptionDto.from_remna_user(parsed_user(line))
        local = self.bot_subscription(remna)

        assert local.user_remna_id == remna.uuid

    def test_a_key_from_the_other_line_still_matches(self) -> None:
        # Same user, panel upgraded underneath: the stored key must still line
        # up with what the panel now reports, or every subscription would look
        # mismatched and be resynced.
        v2 = RemnaSubscriptionDto.from_remna_user(parsed_user("v2"))
        v3 = RemnaSubscriptionDto.from_remna_user(parsed_user("v3"))

        assert v2.uuid == v3.uuid

    def test_a_different_panel_user_does_not_match(self) -> None:
        mine = RemnaSubscriptionDto.from_remna_user(parsed_user("v3"))
        theirs = RemnaSubscriptionDto.from_remna_user(parsed_user("v3", id=999))

        assert mine.uuid != theirs.uuid


class TestApplySync:
    """Copying panel state onto a local subscription, including the key."""

    def local(self, **overrides: Any) -> SubscriptionDto:
        base = SubscriptionDto(
            user_remna_id=encode_remna_id(PANEL_ID),
            status=SubscriptionStatus.ACTIVE,
            traffic_limit=100,
            device_limit=3,
            traffic_limit_strategy="NO_RESET",
            tag=None,
            internal_squads=[UUID(SQUAD_UUID)],
            external_squad=None,
            expire_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
            url=f"https://sub.example.com/{SHORT_UUID}",
            plan_snapshot=a_plan(),
        )
        return replace(base, **overrides) if overrides else base

    def test_the_key_is_copied_from_the_panel_record(self, sdk: object) -> None:
        service = RemnawaveImpl.__new__(RemnawaveImpl)
        remna = RemnaSubscriptionDto.from_remna_user(parsed_user("v3", id=777))
        target = self.local()

        service.apply_sync(target, remna)

        assert decode_remna_id(target.user_remna_id) == 777

    def test_a_legacy_key_is_replaced_by_the_encoded_one(self, sdk: object) -> None:
        # A row the backfill missed gets corrected the next time it syncs.
        service = RemnawaveImpl.__new__(RemnawaveImpl)
        legacy = UUID("6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c")
        target = self.local(user_remna_id=legacy)
        remna = RemnaSubscriptionDto.from_remna_user(parsed_user("v3"))

        service.apply_sync(target, remna)

        assert is_synthetic_remna_id(target.user_remna_id)
        assert decode_remna_id(target.user_remna_id) == PANEL_ID


class TestTrafficConversion:
    """The panel reports traffic as a float on both lines."""

    @pytest.mark.parametrize("line", ["v2", "v3"])
    def test_a_limit_survives_the_round_trip(self, line: str) -> None:
        sub = RemnaSubscriptionDto.from_remna_user(parsed_user(line))
        assert sub.traffic_limit == 100

    def test_unlimited_traffic_reads_as_zero(self) -> None:
        sub = RemnaSubscriptionDto.from_remna_user(
            parsed_user("v3", trafficLimitBytes=0)
        )
        assert sub.traffic_limit == 0

    @pytest.mark.parametrize(
        "gigabytes", [1, 10, 100, 1000, 10_000]
    )
    def test_whole_gigabyte_limits_are_exact(self, gigabytes: int) -> None:
        # A rounding slip here would silently change what a user paid for.
        sub = RemnaSubscriptionDto.from_remna_user(
            parsed_user("v3", trafficLimitBytes=gigabytes * 1024**3)
        )
        assert sub.traffic_limit == gigabytes

    def test_a_float_limit_does_not_raise(self) -> None:
        # The model types this as a float, and the converter wants an int.
        sub = RemnaSubscriptionDto.from_remna_user(
            parsed_user("v3", trafficLimitBytes=107374182400.0)
        )
        assert sub.traffic_limit == 100

    def test_a_petabyte_scale_limit_does_not_overflow(self) -> None:
        assert bytes_to_gb(1024**5) == 1024**2


class TestUuidCache:
    """The 2.x client memoises id -> uuid; the mapping is immutable per panel."""

    async def test_the_cache_is_bounded(self, http: Any) -> None:
        client = PanelClientV2(http)
        payload: Dict[str, Any] = user_payload(line="v2")

        for panel_id in range(1, 25_001):
            client._remember_uuid(panel_id, payload)

        # It clears rather than growing without limit; either way it must not
        # hold more than the cap.
        assert len(client._uuid_by_id) <= 20_000

    async def test_a_remembered_uuid_is_returned_without_a_call(
        self, http: Any, panel: Any
    ) -> None:
        client = PanelClientV2(http)
        client._remember_uuid(PANEL_ID, user_payload(line="v2"))
        before = len(panel.calls)

        await client.get_user_uuid(PANEL_ID)

        assert len(panel.calls) == before

    def test_a_payload_without_a_uuid_is_not_cached(self, http: Any) -> None:
        # A 3.x payload reaching the 2.x client would mean the line was
        # misdetected; caching None would hide that behind a confusing error.
        client = PanelClientV2(http)

        assert client._remember_uuid(PANEL_ID, user_payload(line="v3")) is None
        assert PANEL_ID not in client._uuid_by_id


class TestPlanTypeUnchanged:
    def test_limits_still_classify_the_same_way(self) -> None:
        # Guards against the traffic conversion change shifting plan types.
        sub = RemnaSubscriptionDto.from_remna_user(parsed_user("v3"))
        local = SubscriptionDto(
            user_remna_id=sub.uuid,
            status=sub.status,
            traffic_limit=sub.traffic_limit,
            device_limit=sub.device_limit,
            traffic_limit_strategy=sub.traffic_limit_strategy,
            expire_at=sub.expire_at,
            url=sub.url,
            plan_snapshot=a_plan(),
        )

        assert local.limit_type is PlanType.BOTH
