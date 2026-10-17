"""Expiry notifications have to work on both sides of the 2.8.0 event rename.

Panels up to 2.7.x emit four discrete events and no meta. From 2.8.0 they emit a
single ``user.expiration`` whose ``meta.expiration`` is the hours to expiry,
negative before it and positive after. One build serves both, so both spellings
have to land on the same notification.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from src.application.dto.settings import NotificationsSettingsDto
from src.application.events import SubscriptionExpiresEvent
from src.application.events.user import SubscriptionExpiredAgoEvent
from src.application.services.remnawave import (
    _LEGACY_EXPIRATION_HOURS,
    RemnaUserEvent,
    RemnaWebhookService,
    _expiration_hours,
    _is_expiration_event,
)
from src.core.enums import UserNotificationType


class RecordingBus:
    def __init__(self) -> None:
        self.published: List[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


@dataclass
class FakeRemnaUser:
    expire_at: datetime
    telegram_id: Optional[int] = 123456789


@dataclass
class FakeSubscription:
    expire_at: datetime
    is_trial: bool = False


def make_service(bus: RecordingBus) -> RemnaWebhookService:
    # Only the event bus participates in the expiry path; the rest of the
    # collaborators are never reached, so the service is built bare.
    service = RemnaWebhookService.__new__(RemnaWebhookService)
    service.event_bus = bus  # type: ignore[attr-defined]
    return service


EXPIRE_AT = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


async def run(bus: RecordingBus, hours: Optional[int]) -> List[Any]:
    service = make_service(bus)
    await service._process_expiration(
        user=object(),  # type: ignore[arg-type]
        current_subscription=FakeSubscription(expire_at=EXPIRE_AT),  # type: ignore[arg-type]
        remna_user=FakeRemnaUser(expire_at=EXPIRE_AT),  # type: ignore[arg-type]
        expiration_hours=hours,
    )
    return bus.published


class TestLegacyEventMapping:
    def test_every_legacy_event_maps_onto_the_new_scale(self) -> None:
        # Negative before the expiry, positive after -- the convention
        # meta.expiration uses from 2.8.0 on.
        assert _LEGACY_EXPIRATION_HOURS == {
            RemnaUserEvent.EXPIRES_IN_72_HOURS: -72,
            RemnaUserEvent.EXPIRES_IN_48_HOURS: -48,
            RemnaUserEvent.EXPIRES_IN_24_HOURS: -24,
            RemnaUserEvent.EXPIRED_24_HOURS_AGO: 24,
        }

    def test_both_vocabularies_are_recognised_as_expiry_events(self) -> None:
        assert _is_expiration_event(RemnaUserEvent.EXPIRATION)
        for event in _LEGACY_EXPIRATION_HOURS:
            assert _is_expiration_event(event)

    def test_unrelated_events_are_not_expiry_events(self) -> None:
        for event in [
            RemnaUserEvent.CREATED,
            RemnaUserEvent.DELETED,
            RemnaUserEvent.EXPIRED,
            RemnaUserEvent.FIRST_CONNECTED,
        ]:
            assert not _is_expiration_event(event)

    def test_the_new_event_takes_its_hours_from_the_panel(self) -> None:
        assert _expiration_hours(RemnaUserEvent.EXPIRATION, -48) == -48

    def test_a_legacy_event_ignores_any_reported_hours(self) -> None:
        # An older panel sends no meta at all; the event name is the whole story.
        assert _expiration_hours(RemnaUserEvent.EXPIRES_IN_72_HOURS, None) == -72
        assert _expiration_hours(RemnaUserEvent.EXPIRED_24_HOURS_AGO, 999) == 24

    def test_the_legacy_event_names_match_the_panel_contract(self) -> None:
        # Straight from libs/contract at 2.7.4.
        assert RemnaUserEvent.EXPIRES_IN_72_HOURS == "user.expires_in_72_hours"
        assert RemnaUserEvent.EXPIRES_IN_48_HOURS == "user.expires_in_48_hours"
        assert RemnaUserEvent.EXPIRES_IN_24_HOURS == "user.expires_in_24_hours"
        assert RemnaUserEvent.EXPIRED_24_HOURS_AGO == "user.expired_24_hours_ago"

    def test_the_new_event_name_matches_the_panel_contract(self) -> None:
        # Introduced in 2.8.0, still current in 3.4.x.
        assert RemnaUserEvent.EXPIRATION == "user.expiration"

    def test_the_two_sets_do_not_overlap(self) -> None:
        assert RemnaUserEvent.EXPIRATION not in _LEGACY_EXPIRATION_HOURS


class TestUpcomingExpiry:
    @pytest.mark.parametrize(
        "hours, expected_day",
        [(-72, 3), (-48, 2), (-24, 1), (-1, 1), (-12, 1), (-96, 4)],
    )
    async def test_negative_hours_announce_an_upcoming_expiry(
        self, bus: RecordingBus, hours: int, expected_day: int
    ) -> None:
        published = await run(bus, hours)

        assert len(published) == 1
        assert isinstance(published[0], SubscriptionExpiresEvent)
        assert published[0].day == expected_day

    async def test_the_legacy_events_produce_the_same_days_as_before(
        self, bus: RecordingBus
    ) -> None:
        # 2.7.4 mapped 72/48/24 hours onto days 3/2/1; that must not shift.
        for event, hours in _LEGACY_EXPIRATION_HOURS.items():
            if hours > 0:
                continue
            bus.published.clear()
            published = await run(bus, hours)
            assert published[0].day == abs(hours) // 24, event


class TestPastExpiry:
    @pytest.mark.parametrize("hours, expected_day", [(24, 1), (48, 2), (1, 1), (72, 3)])
    async def test_positive_hours_announce_a_past_expiry(
        self, bus: RecordingBus, hours: int, expected_day: int
    ) -> None:
        published = await run(bus, hours)

        assert len(published) == 1
        assert isinstance(published[0], SubscriptionExpiredAgoEvent)
        assert published[0].day == expected_day

    async def test_the_legacy_expired_ago_event_still_means_one_day(
        self, bus: RecordingBus
    ) -> None:
        # 2.7.4's user.expired_24_hours_ago is folded in as +24 hours.
        published = await run(bus, 24)
        assert isinstance(published[0], SubscriptionExpiredAgoEvent)
        assert published[0].day == 1


class TestSuppression:
    async def test_a_renewed_subscription_is_not_announced(
        self, bus: RecordingBus
    ) -> None:
        # The panel can emit an expiry that the user has already paid past; the
        # local record is newer, so the notification would be wrong.
        service = make_service(bus)
        await service._process_expiration(
            user=object(),  # type: ignore[arg-type]
            current_subscription=FakeSubscription(  # type: ignore[arg-type]
                expire_at=EXPIRE_AT + timedelta(days=30)
            ),
            remna_user=FakeRemnaUser(expire_at=EXPIRE_AT),  # type: ignore[arg-type]
            expiration_hours=-24,
        )

        assert bus.published == []

    async def test_a_missing_meta_value_announces_nothing(
        self, bus: RecordingBus
    ) -> None:
        # A 2.8.0+ panel with meta stripped by a proxy must not produce a
        # notification with an invented day count.
        assert await run(bus, None) == []

    async def test_a_small_clock_skew_still_announces(self, bus: RecordingBus) -> None:
        # Under an hour of drift between panel and bot is not a renewal.
        service = make_service(bus)
        await service._process_expiration(
            user=object(),  # type: ignore[arg-type]
            current_subscription=FakeSubscription(  # type: ignore[arg-type]
                expire_at=EXPIRE_AT + timedelta(minutes=30)
            ),
            remna_user=FakeRemnaUser(expire_at=EXPIRE_AT),  # type: ignore[arg-type]
            expiration_hours=-24,
        )

        assert len(bus.published) == 1


class TestNotificationToggles:
    """Each "expires in N days" notice obeys its own settings toggle."""

    @pytest.mark.parametrize(
        "hours, expected",
        [
            (-72, UserNotificationType.EXPIRES_IN_3_DAYS),
            (-48, UserNotificationType.EXPIRES_IN_2_DAYS),
            (-24, UserNotificationType.EXPIRES_IN_1_DAY),
            (-6, UserNotificationType.EXPIRES_IN_1_DAY),
        ],
    )
    async def test_the_notice_names_the_toggle_for_its_day(
        self, bus: RecordingBus, hours: int, expected: UserNotificationType
    ) -> None:
        # It used to always claim the one-day toggle: switching off the 3- or
        # 2-day notice did nothing, and switching off the 1-day one silenced all.
        published = await run(bus, hours)

        assert published[0].notification_type is expected

    @pytest.mark.parametrize("hours", [-96, -120, -168])
    async def test_a_warning_further_out_than_three_days_uses_the_3_day_toggle(
        self, bus: RecordingBus, hours: int
    ) -> None:
        # From 2.8.0 the panel can be set to warn up to a week ahead.
        published = await run(bus, hours)

        assert published[0].notification_type is UserNotificationType.EXPIRES_IN_3_DAYS

    async def test_an_expired_notice_keeps_its_own_toggle(self, bus: RecordingBus) -> None:
        published = await run(bus, 24)

        assert published[0].notification_type is UserNotificationType.EXPIRED_1_DAY_AGO

    @pytest.mark.parametrize(
        "toggle_off, hours, delivered",
        [
            (UserNotificationType.EXPIRES_IN_3_DAYS, -72, False),
            (UserNotificationType.EXPIRES_IN_3_DAYS, -24, True),
            (UserNotificationType.EXPIRES_IN_1_DAY, -72, True),
            (UserNotificationType.EXPIRES_IN_1_DAY, -24, False),
        ],
    )
    async def test_switching_one_notice_off_leaves_the_others(
        self,
        bus: RecordingBus,
        toggle_off: UserNotificationType,
        hours: int,
        delivered: bool,
    ) -> None:
        settings = NotificationsSettingsDto(settings={toggle_off: False})
        published = await run(bus, hours)

        assert settings.is_enabled(published[0].notification_type) is delivered
