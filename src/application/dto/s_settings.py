"""Settings of the "s" edition (docs/s/README.md).

They live under ``settings.extra.s`` so that the edition adds no column and no
migration to the upstream ``settings`` table: a row written before these fields
existed loads with the defaults below. Every feature defaults to off, so an
"s" build behaves like 3x until something is switched on.
"""

from dataclasses import dataclass, field
from typing import Any

from .base import TrackableMixin


@dataclass(kw_only=True)
class SlotsSettingsDto(TrackableMixin):
    enabled: bool = False
    # The panel limit never drops below this, even with nothing connected.
    base_limit: int = 1
    slot_ttl_minutes: int = 5


@dataclass(kw_only=True)
class ClientVersionsSettingsDto(TrackableMixin):
    enabled: bool = False
    refresh_hours: int = 6
    # A version seen on fewer devices than this is not trusted as released.
    observed_min_devices: int = 2
    # A gap only in the last number (3.5.5 -> 3.5.7) is not worth a message.
    ignore_patch: bool = True
    min_minor_gap: int = 1
    notify_enabled: bool = False
    interval_days: int = 7
    # Admin edits over the built-in sources: {app: {channel: {key: value}}}.
    sources: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


@dataclass(kw_only=True)
class StaleSubscriptionSettingsDto(TrackableMixin):
    enabled: bool = False
    stale_hours: int = 72
    interval_days: int = 7


@dataclass(kw_only=True)
class InactiveCleanupSettingsDto(TrackableMixin):
    enabled: bool = False
    delete_after_days: int = 30
    warn_days: int = 3
    include_disabled: bool = False


@dataclass(kw_only=True)
class ChatAccessChatDto:
    chat_id: int
    title: str = ""


@dataclass(kw_only=True)
class ChatAccessSettingsDto(TrackableMixin):
    enabled: bool = False
    chats: list[ChatAccessChatDto] = field(default_factory=list)
    grace_hours: int = 24


@dataclass(kw_only=True)
class InviteLinksSettingsDto(TrackableMixin):
    """One-time referral links: each opens the bot for one new user, then dies.

    While on, the permanent ``ref_<code>`` links stop bringing referrals.
    """

    enabled: bool = False
    ttl_hours: int = 24
    max_active: int = 3


@dataclass(kw_only=True)
class PlanTermsSettingsDto(TrackableMixin):
    """How a plan's duration turns into an expiry date.

    With ``calendar_months`` a duration named in months or years (a multiple
    of 30 or 365 days) ends on the same day that many months later.
    """

    calendar_months: bool = False


@dataclass(kw_only=True)
class RulesTextSettingsDto(TrackableMixin):
    """The rules themselves, shown in Telegram instead of a link.

    HTML as Telegram formats it (``Message.html_text``); empty means upstream's
    link is used.
    """

    text: str = ""


@dataclass(kw_only=True)
class SSettingsDto(TrackableMixin):
    slots: SlotsSettingsDto = field(default_factory=SlotsSettingsDto)
    versions: ClientVersionsSettingsDto = field(default_factory=ClientVersionsSettingsDto)
    stale: StaleSubscriptionSettingsDto = field(default_factory=StaleSubscriptionSettingsDto)
    inactive: InactiveCleanupSettingsDto = field(default_factory=InactiveCleanupSettingsDto)
    chat_access: ChatAccessSettingsDto = field(default_factory=ChatAccessSettingsDto)
    invites: InviteLinksSettingsDto = field(default_factory=InviteLinksSettingsDto)
    plans: PlanTermsSettingsDto = field(default_factory=PlanTermsSettingsDto)
    rules: RulesTextSettingsDto = field(default_factory=RulesTextSettingsDto)
    # HWID values that mark a device as a router (MikroTik container and the
    # like). A router takes a regular slot; it is only left out of reminders
    # that make no sense for it.
    router_hwids: list[str] = field(default_factory=list)
