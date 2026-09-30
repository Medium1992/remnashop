"""How an incoming event is routed, and what it looks the user up by.

The routing is the part of the webhook path that changed most: the handler used
to find the local account by the panel's uuid, which no longer exists on 3.x.
Everything here drives ``handle_user_event`` with a payload from each panel line
and checks both that the right thing happens and that the lookup key is the one
that survives an upgrade.
"""

from typing import Any, Dict, List, Optional

import pytest
from conftest import PANEL_ID, user_payload
from test_identity_invariants import parsed_user
from test_service_end_to_end import a_subscription

from src.application.dto import UserDto
from src.application.events import (
    SubscriptionLimitedEvent,
    UserFirstConnectionEvent,
)
from src.application.services.remnawave import RemnaUserEvent, RemnaWebhookService
from src.core.utils.remna_id import decode_remna_id, remna_key

LINES = ["v2", "v3"]


class Recorder:
    def __init__(self) -> None:
        self.published: List[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


class FakeUserDao:
    def __init__(self, known: bool = True) -> None:
        self.known = known
        self.looked_up: List[Any] = []

    async def get_by_remna_uuid(self, key: Any) -> Optional[Any]:
        self.looked_up.append(key)
        if not self.known:
            return None
        return _user()


class FakeSubscriptionDao:
    def __init__(self, present: bool = True) -> None:
        self.present = present

    async def get_current(self, user_id: int) -> Optional[Any]:
        return _subscription() if self.present else None


class FakeSyncUser:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def system(self, dto: Any) -> None:
        self.calls.append(dto)


def _user() -> Any:
    return UserDto(id=7, telegram_id=123456789, name="Test", username="tester")


def _subscription() -> Any:
    return a_subscription()


def build(
    *, known_user: bool = True, has_subscription: bool = True
) -> tuple[RemnaWebhookService, Recorder, FakeUserDao, FakeSyncUser]:
    service = RemnaWebhookService.__new__(RemnaWebhookService)
    bus = Recorder()
    user_dao = FakeUserDao(known=known_user)
    sync_user = FakeSyncUser()

    service.event_bus = bus  # type: ignore[attr-defined]
    service.user_dao = user_dao  # type: ignore[attr-defined]
    service.subscription_dao = FakeSubscriptionDao(has_subscription)  # type: ignore[attr-defined]
    service.sync_user = sync_user  # type: ignore[attr-defined]

    return service, bus, user_dao, sync_user


class TestLookupKey:
    @pytest.mark.parametrize("line", LINES)
    async def test_the_user_is_looked_up_by_the_encoded_id(self, line: str) -> None:
        # Not by the panel uuid: on 3.x there is none, and on 2.x using it would
        # key the same person differently before and after an upgrade.
        service, _, user_dao, _ = build()

        await service.handle_user_event(RemnaUserEvent.ENABLED, parsed_user(line))

        assert len(user_dao.looked_up) == 1
        assert decode_remna_id(user_dao.looked_up[0]) == PANEL_ID

    @pytest.mark.parametrize("line", LINES)
    async def test_both_lines_look_up_the_same_key(self, line: str) -> None:
        service, _, user_dao, _ = build()

        await service.handle_user_event(RemnaUserEvent.ENABLED, parsed_user("v2"))
        await service.handle_user_event(RemnaUserEvent.ENABLED, parsed_user("v3"))

        assert user_dao.looked_up[0] == user_dao.looked_up[1]

    async def test_an_unknown_user_is_dropped_quietly(self) -> None:
        service, bus, _, sync_user = build(known_user=False)

        await service.handle_user_event(RemnaUserEvent.ENABLED, parsed_user("v3"))

        assert bus.published == []
        assert sync_user.calls == []

    async def test_a_user_without_a_current_subscription_is_dropped(self) -> None:
        service, bus, _, sync_user = build(has_subscription=False)

        await service.handle_user_event(RemnaUserEvent.LIMITED, parsed_user("v3"))

        assert bus.published == []


class TestStatusEvents:
    @pytest.mark.parametrize("line", LINES)
    @pytest.mark.parametrize(
        "event",
        [
            RemnaUserEvent.ENABLED,
            RemnaUserEvent.DISABLED,
            RemnaUserEvent.LIMITED,
            RemnaUserEvent.REVOKED,
        ],
    )
    async def test_a_status_event_resyncs_the_local_record(
        self, line: str, event: str
    ) -> None:
        service, _, _, sync_user = build()

        await service.handle_user_event(event, parsed_user(line))

        assert len(sync_user.calls) == 1
        assert sync_user.calls[0].creating is False

    @pytest.mark.parametrize("line", LINES)
    async def test_a_limit_reached_notifies_the_user(self, line: str) -> None:
        service, bus, _, _ = build()

        await service.handle_user_event(RemnaUserEvent.LIMITED, parsed_user(line))

        assert any(isinstance(e, SubscriptionLimitedEvent) for e in bus.published)

    @pytest.mark.parametrize("line", LINES)
    async def test_enabling_does_not_notify(self, line: str) -> None:
        # Enabling is a state change the user did not ask about.
        service, bus, _, _ = build()

        await service.handle_user_event(RemnaUserEvent.ENABLED, parsed_user(line))

        assert bus.published == []


class TestFirstConnection:
    @pytest.mark.parametrize("line", LINES)
    async def test_a_first_connection_carries_the_encoded_key(self, line: str) -> None:
        # The event exposes the identity to the notification layer; a raw panel
        # uuid there would be meaningless on 3.x.
        service, bus, _, _ = build()

        await service.handle_user_event(RemnaUserEvent.FIRST_CONNECTED, parsed_user(line))

        published = [e for e in bus.published if isinstance(e, UserFirstConnectionEvent)]
        assert len(published) == 1
        assert decode_remna_id(published[0].subscription_id) == PANEL_ID

    @pytest.mark.parametrize("line", LINES)
    async def test_the_traffic_figures_are_rendered(self, line: str) -> None:
        service, bus, _, _ = build()

        await service.handle_user_event(RemnaUserEvent.FIRST_CONNECTED, parsed_user(line))

        event = bus.published[0]
        assert event.traffic_used
        assert event.expire_time


class TestAdoptionGate:
    @pytest.mark.parametrize("line", LINES)
    async def test_a_created_user_without_the_tag_is_ignored(self, line: str) -> None:
        # Otherwise RemnaShop would adopt every account an operator creates by
        # hand in the panel.
        service, _, _, sync_user = build()
        untagged: Dict[str, Any] = user_payload(line=line, tag=None)

        await service.handle_user_event(
            RemnaUserEvent.CREATED, parsed_user(line, tag=None)
        )

        assert sync_user.calls == []
        assert untagged["tag"] is None

    @pytest.mark.parametrize("line", LINES)
    async def test_a_created_user_tagged_imported_is_adopted(self, line: str) -> None:
        service, _, _, sync_user = build()

        await service.handle_user_event(
            RemnaUserEvent.CREATED, parsed_user(line, tag="IMPORTED")
        )

        assert len(sync_user.calls) == 1
        assert sync_user.calls[0].creating is True

    @pytest.mark.parametrize("line", LINES)
    async def test_a_modified_user_is_always_synced(self, line: str) -> None:
        # The gate is only on creation; an account already adopted keeps syncing
        # whatever its tag says now.
        service, _, _, sync_user = build()

        await service.handle_user_event(
            RemnaUserEvent.MODIFIED, parsed_user(line, tag=None)
        )

        assert len(sync_user.calls) == 1
        assert sync_user.calls[0].creating is False


class TestUnknownEvents:
    @pytest.mark.parametrize("line", LINES)
    async def test_an_event_this_build_does_not_know_is_ignored(self, line: str) -> None:
        # A newer panel will invent events; they must not raise, or the endpoint
        # answers an error and the panel retries forever.
        service, bus, _, _ = build()

        await service.handle_user_event("user.something_new", parsed_user(line))

        assert bus.published == []

    @pytest.mark.parametrize("line", LINES)
    async def test_the_bandwidth_threshold_event_is_ignored_quietly(
        self, line: str
    ) -> None:
        service, bus, _, _ = build()

        await service.handle_user_event(
            RemnaUserEvent.BANDWIDTH_USAGE_THRESHOLD_REACHED, parsed_user(line)
        )

        assert bus.published == []


class TestKeyDerivationMatchesStorage:
    @pytest.mark.parametrize("line", LINES)
    async def test_the_lookup_key_equals_what_creation_would_store(
        self, line: str
    ) -> None:
        # If these ever diverge, a webhook would never find the account the
        # purchase created.
        service, _, user_dao, _ = build()

        await service.handle_user_event(RemnaUserEvent.ENABLED, parsed_user(line))

        assert user_dao.looked_up[0] == remna_key(parsed_user(line))
