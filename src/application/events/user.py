from dataclasses import asdict, dataclass, field
from typing import Any

from remnapy.enums.users import TrafficLimitStrategy

from src.application.dto import MessagePayloadDto
from src.core.enums import MessageEffectId, ReferralRewardType, UserNotificationType
from src.core.types import NotificationType

from .base import UserEvent


@dataclass(frozen=True, kw_only=True)
class SubscriptionLimitedEvent(UserEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.LIMITED,
        init=False,
    )

    is_trial: bool
    traffic_strategy: TrafficLimitStrategy
    reset_time: Any

    @property
    def event_key(self) -> str:
        return "event-subscription.limited"

    def as_payload(self) -> "MessagePayloadDto":
        return MessagePayloadDto(
            i18n_key=self.event_key,
            i18n_kwargs={**asdict(self)},
            disable_default_markup=False,
            delete_after=None,
        )


@dataclass(frozen=True, kw_only=True)
class SubscriptionExpiredEvent(UserEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.EXPIRED,
        init=True,
    )

    is_trial: bool

    @property
    def event_key(self) -> str:
        return "event-subscription.expired"

    def as_payload(self) -> "MessagePayloadDto":
        return MessagePayloadDto(
            i18n_key=self.event_key,
            i18n_kwargs={**asdict(self)},
            disable_default_markup=False,
            delete_after=None,
        )


@dataclass(frozen=True, kw_only=True)
class SubscriptionExpiredAgoEvent(UserEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.EXPIRED_1_DAY_AGO,
        init=True,
    )

    is_trial: bool
    day: int

    @property
    def event_key(self) -> str:
        return "event-subscription.expired-ago"

    def as_payload(self) -> "MessagePayloadDto":
        return MessagePayloadDto(
            i18n_key=self.event_key,
            i18n_kwargs={**asdict(self), "value": self.day},
            disable_default_markup=False,
            delete_after=None,
        )


@dataclass(frozen=True, kw_only=True)
class SubscriptionExpiresEvent(UserEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.EXPIRES_IN_1_DAY,
        init=True,
    )

    is_trial: bool
    day: int

    @property
    def event_key(self) -> str:
        return "event-subscription.expiring"

    def as_payload(self) -> "MessagePayloadDto":
        return MessagePayloadDto(
            i18n_key=self.event_key,
            i18n_kwargs={**asdict(self), "value": self.day},
            disable_default_markup=False,
            delete_after=None,
        )


@dataclass(frozen=True, kw_only=True)
class TorrentBlockedEvent(UserEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.TORRENT_BLOCKED,
        init=False,
    )

    node_name: str
    block_duration: Any
    support_url: str
    # s edition: what was seen, so the customer can find the program behind it
    # "none" when the node didn't say
    protocol: str = "unknown"
    network: str = "none"
    source: str = "none"
    destination: str = "none"  # the IP, with the port when known
    domain: str = "none"  # what the node sniffed, when it did

    @property
    def event_key(self) -> str:
        return "event-torrent-blocker.user-blocked"


@dataclass(frozen=True, kw_only=True)
class ReferralEvent(UserEvent):
    name: str


@dataclass(frozen=True, kw_only=True)
class ReferralAttachedEvent(ReferralEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.REFERRAL_ATTACHED,
        init=True,
    )

    @property
    def event_key(self) -> str:
        return "event-referral.attached"


@dataclass(frozen=True, kw_only=True)
class ReferralRewardEvent(ReferralEvent):
    value: int
    reward_type: ReferralRewardType


@dataclass(frozen=True, kw_only=True)
class ReferralRewardReceivedEvent(ReferralRewardEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.REFERRAL_REWARD_RECEIVED,
        init=True,
    )

    @property
    def event_key(self) -> str:
        return "event-referral.reward"

    def as_payload(self) -> "MessagePayloadDto":
        return MessagePayloadDto(
            i18n_key=self.event_key,
            i18n_kwargs={**asdict(self)},
            disable_default_markup=False,
            delete_after=None,
            message_effect=MessageEffectId.PARTY,
        )


@dataclass(frozen=True, kw_only=True)
class ReferralRewardFailedEvent(ReferralRewardEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.REFERRAL_REWARD_FAILED,
        init=True,
    )

    @property
    def event_key(self) -> str:
        return "event-referral.reward-failed"


@dataclass(frozen=True, kw_only=True)
class UserNotConnectedEvent(UserEvent):
    notification_type: NotificationType = field(
        default=UserNotificationType.NOT_CONNECTED,
        init=False,
    )

    support_url: str

    @property
    def event_key(self) -> str:
        return "event-subscription.not-connected"

    def as_payload(self) -> "MessagePayloadDto":
        return MessagePayloadDto(
            i18n_key=self.event_key,
            disable_default_markup=True,
            delete_after=None,
        )
