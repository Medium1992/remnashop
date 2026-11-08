"""Strict HWID slots: the panel never holds a free place nobody asked for.

The plan's device limit becomes a ceiling. The panel limit is kept at what is
occupied (but at least ``base_limit``), so a subscription link that leaks
cannot quietly connect another device. To add one, the owner opens a
temporary slot in the bot: +1 in the panel for ``slot_ttl_minutes``. A device
that appears in that window keeps the place; otherwise the limit goes back.

A panel that does not answer changes nothing: an empty device list caused by a
500 must never be read as "nothing occupied".
"""

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from enum import StrEnum, auto
from typing import Optional
from uuid import UUID

from loguru import logger
from redis.asyncio import Redis

from src.application.common import Notifier
from src.application.common.dao import SettingsDao, SubscriptionDao, UserDao
from src.application.dto import MessagePayloadDto, SubscriptionDto, UserDto
from src.application.dto.s_settings import SlotsSettingsDto
from src.application.events.system import UserDeviceAddedEvent, UserDeviceDeletedEvent
from src.core.enums import SubscriptionStatus
from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import SlotGrantKey
from src.infrastructure.services.event_bus import on_event

from .remnawave import SRemnawave
from .slot_grants import (
    MAX_LOOKUP_MISSES,
    PANEL_CONCURRENCY,
    SlotGrant,
    grant_key,
    physical_limit,
    read_grant,
)


class SlotOutcome(StrEnum):
    OPENED = auto()
    ALREADY_OPEN = auto()
    FREE_PLACE = auto()  # the panel already has room: nothing to open
    AT_CEILING = auto()
    UNLIMITED = auto()
    UNAVAILABLE = auto()
    DISABLED = auto()
    BLOCKED = auto()  # the subscription is switched off: a slot would not help


@dataclass(frozen=True)
class SlotResult:
    outcome: SlotOutcome
    seconds_left: int = 0
    ceiling: int = 0


class SlotService:
    def __init__(
        self,
        redis: Redis,
        remnawave: SRemnawave,
        settings_dao: SettingsDao,
        subscription_dao: SubscriptionDao,
        user_dao: UserDao,
        notifier: Notifier,
    ) -> None:
        self.redis = redis
        self.remnawave = remnawave
        self.settings_dao = settings_dao
        self.subscription_dao = subscription_dao
        self.user_dao = user_dao
        self.notifier = notifier

    async def _settings(self) -> SlotsSettingsDto:
        return (await self.settings_dao.get()).extra.s.slots

    # ── grants ──────────────────────────────────────────────────────────────

    async def get_grant(self, remna_id: UUID) -> Optional[SlotGrant]:
        return await read_grant(self.redis, remna_id)

    async def _save_grant(self, remna_id: UUID, grant: SlotGrant) -> None:
        # Kept past its expiry so the sweep still finds and closes it.
        ttl = max(60, int(grant.expires_at - time.time())) + 86400
        await self.redis.set(grant_key(remna_id), json.dumps(asdict(grant)), ex=ttl)

    async def _drop_grant(self, remna_id: UUID) -> None:
        await self.redis.delete(grant_key(remna_id))

    # ── user action ─────────────────────────────────────────────────────────

    async def open_slot(
        self, user: UserDto, subscription: SubscriptionDto, *, by_admin: bool = False
    ) -> SlotResult:
        """+1 in the panel for the slot's lifetime.

        ``by_admin`` may go past the plan's ceiling: an admin granting a place
        is a deliberate exception.
        """
        settings = await self._settings()
        if not settings.enabled:
            return SlotResult(SlotOutcome.DISABLED)
        ceiling = subscription.device_limit
        if ceiling <= 0:
            return SlotResult(SlotOutcome.UNLIMITED)
        if subscription.status == SubscriptionStatus.DISABLED and not by_admin:
            return SlotResult(SlotOutcome.BLOCKED)

        grant = await self.get_grant(subscription.user_remna_id)
        if grant and grant.is_open:
            return SlotResult(SlotOutcome.ALREADY_OPEN, seconds_left=grant.seconds_left)
        if grant:
            await self._close(subscription, grant, notify=False)

        try:
            devices = await self.remnawave.get_devices(subscription.user_remna_id)
        except Exception as e:
            logger.warning(f"Slot for {user.log} not opened, device list unavailable: {e}")
            return SlotResult(SlotOutcome.UNAVAILABLE)

        occupied = len(devices)
        current = physical_limit(ceiling, occupied, settings.base_limit)
        if current > occupied and not by_admin:
            return SlotResult(SlotOutcome.FREE_PLACE, ceiling=ceiling)
        if occupied >= ceiling and not by_admin:
            return SlotResult(SlotOutcome.AT_CEILING, ceiling=ceiling)

        granted = max(current, occupied) + 1
        try:
            await self.remnawave.set_device_limit(subscription.user_remna_id, granted)
        except Exception as e:
            logger.warning(f"Slot for {user.log} not opened, panel refused: {e}")
            return SlotResult(SlotOutcome.UNAVAILABLE)

        ttl = settings.slot_ttl_minutes * 60
        await self._save_grant(
            subscription.user_remna_id,
            SlotGrant(
                telegram_id=user.telegram_id,
                original_limit=current,
                granted_limit=granted,
                device_count_before=occupied,
                expires_at=time.time() + ttl,
            ),
        )
        logger.info(
            f"Slot opened for {user.log}: limit {current} -> {granted} for {ttl}s"
            + (" (by admin)" if by_admin else "")
        )
        return SlotResult(SlotOutcome.OPENED, seconds_left=ttl, ceiling=ceiling)

    # ── closing ─────────────────────────────────────────────────────────────

    async def _close(
        self,
        subscription: SubscriptionDto,
        grant: SlotGrant,
        *,
        notify: bool,
        device_count: Optional[int] = None,
    ) -> bool:
        """Settle one grant. False when the panel did not answer (kept for later)."""
        settings = await self._settings()
        try:
            if device_count is None:
                device_count = len(await self.remnawave.get_devices(subscription.user_remna_id))
            consumed = device_count > grant.device_count_before
            target = physical_limit(subscription.device_limit, device_count, settings.base_limit)
            if subscription.device_limit <= 0:
                # The plan went unlimited meanwhile: nothing to hold back.
                target = 0
            await self.remnawave.set_device_limit(subscription.user_remna_id, target)
        except Exception as e:
            grant.misses += 1
            if grant.misses >= MAX_LOOKUP_MISSES:
                logger.warning(f"Slot grant '{subscription.user_remna_id}' dropped after misses")
                await self._drop_grant(subscription.user_remna_id)
                return True
            logger.warning(f"Slot grant '{subscription.user_remna_id}' kept, panel failed: {e}")
            await self._save_grant(subscription.user_remna_id, grant)
            return False

        await self._drop_grant(subscription.user_remna_id)
        logger.info(
            f"Slot for '{subscription.user_remna_id}' "
            f"{'consumed' if consumed else 'expired'}: limit -> {target}"
        )
        if notify and grant.telegram_id:
            await self._notify(
                grant.telegram_id,
                "ntf-s-slots.consumed" if consumed else "ntf-s-slots.expired",
                {"value": settings.slot_ttl_minutes},
            )
        return True

    async def _notify(self, telegram_id: int, key: str, kwargs: dict) -> None:
        user = await self.user_dao.get_by_telegram_id(telegram_id)
        if not user:
            return
        try:
            await self.notifier.notify_user(
                user,
                payload=MessagePayloadDto(i18n_key=key, i18n_kwargs=kwargs, delete_after=None),
            )
        except Exception as e:
            logger.warning(f"Slot notice to {user.log} failed: {e}")

    async def close_expired(self) -> int:
        """Settle every grant whose time is up. Returns how many were settled."""
        settled = 0
        pattern = serialize_storage_key(SlotGrantKey(remna_id="*"))
        async for key in self.redis.scan_iter(match=pattern):
            remna_id = UUID(str(key).rsplit(":", 1)[-1])
            grant = await self.get_grant(remna_id)
            if not grant or grant.is_open:
                continue
            subscription = await self.subscription_dao.get_by_remna_id(remna_id)
            if not subscription:
                await self._drop_grant(remna_id)
                continue
            if await self._close(subscription, grant, notify=True):
                settled += 1
        return settled

    # ── panel events ────────────────────────────────────────────────────────

    async def _event_subscription(self, user_id: int) -> Optional[SubscriptionDto]:
        if not (await self._settings()).enabled:
            return None
        return await self.subscription_dao.get_current(user_id)

    @on_event(UserDeviceAddedEvent)
    async def on_device_added(self, event: UserDeviceAddedEvent) -> None:
        """A device came in while a slot was open: the place is taken, close early."""
        subscription = await self._event_subscription(event.user_id)
        if not subscription:
            return
        grant = await self.get_grant(subscription.user_remna_id)
        if not grant:
            return
        try:
            count = len(await self.remnawave.get_devices(subscription.user_remna_id))
        except Exception as e:
            logger.warning(f"Slot for '{subscription.user_remna_id}' left to the sweep: {e}")
            return
        if count > grant.device_count_before:
            await self._close(subscription, grant, notify=True, device_count=count)

    @on_event(UserDeviceDeletedEvent)
    async def on_device_deleted(self, event: UserDeviceDeletedEvent) -> None:
        """A device left: its place must not stay free in the panel."""
        subscription = await self._event_subscription(event.user_id)
        if subscription:
            await self.reconcile(subscription)

    # ── sweep ───────────────────────────────────────────────────────────────

    async def reconcile(
        self, subscription: SubscriptionDto, settings: Optional[SlotsSettingsDto] = None
    ) -> bool:
        """Bring the panel limit down to what is occupied. True if it changed.

        Leaves a user with an open slot alone, and never acts on a device list
        the panel did not return. ``settings`` is passed in by callers running
        several of these at once, which must not share the database session.
        """
        settings = settings or await self._settings()
        if not settings.enabled or subscription.device_limit <= 0:
            return False
        grant = await self.get_grant(subscription.user_remna_id)
        if grant and grant.is_open:
            return False
        try:
            remna_user = await self.remnawave.get_user_by_uuid(subscription.user_remna_id)
            if remna_user is None:
                return False
            devices = await self.remnawave.get_devices(subscription.user_remna_id)
        except Exception as e:
            logger.warning(f"Reconcile of '{subscription.user_remna_id}' skipped: {e}")
            return False

        target = physical_limit(subscription.device_limit, len(devices), settings.base_limit)
        current = remna_user.hwid_device_limit or 0
        if current == target:
            return False
        try:
            await self.remnawave.set_device_limit(subscription.user_remna_id, target)
        except Exception as e:
            logger.warning(f"Reconcile of '{subscription.user_remna_id}' refused: {e}")
            return False
        logger.info(
            f"Slots reconcile '{subscription.user_remna_id}': limit {current} -> {target}, "
            f"occupied {len(devices)}"
        )
        return True

    async def sweep(self) -> int:
        """Reconcile every active subscriber. Returns how many limits changed."""
        settings = await self._settings()
        if not settings.enabled:
            return 0
        subscriptions = []
        for user in await self.user_dao.get_with_active_subscription():
            subscription = await self.subscription_dao.get_current(user.id)
            if subscription and subscription.is_active and subscription.device_limit > 0:
                subscriptions.append(subscription)

        semaphore = asyncio.Semaphore(PANEL_CONCURRENCY)

        async def one(subscription: SubscriptionDto) -> bool:
            async with semaphore:
                return await self.reconcile(subscription, settings)

        changed = sum(await asyncio.gather(*(one(s) for s in subscriptions)))
        logger.info(f"Slots sweep: '{changed}' of '{len(subscriptions)}' limits changed")
        return changed
