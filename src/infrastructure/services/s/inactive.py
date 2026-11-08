"""Inactive profiles: the list, and deletion after a lapse.

A profile is inactive once its subscription has expired (or, if asked,
been disabled). ``delete_after_days`` later the panel user is deleted -- link,
devices and all -- and the subscription marked DELETED; the bot account stays,
and a new purchase issues a new link.

Nobody is deleted without warning. The warning goes out ``warn_days`` ahead,
and deletion waits at least that long after the warning actually went out:
switching the feature on with a backlog of long-expired users warns them all
first instead of deleting them on the spot.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common import Notifier
from src.application.common.dao import SettingsDao, SubscriptionDao, UserDao
from src.application.dto import MessagePayloadDto, SubscriptionDto, UserDto
from src.application.dto.s_settings import InactiveCleanupSettingsDto
from src.application.events.user import SubscriptionExpiredEvent
from src.application.use_cases.subscription.commands.management import DeleteSubscription
from src.core.enums import SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.database.models import Subscription, User
from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import InactiveListKey, InactiveWarningKey
from src.infrastructure.services.event_bus import on_event
from src.telegram.keyboards import get_buy_keyboard, get_renew_keyboard

from .remnawave import SRemnawave

PANEL_CONCURRENCY = 5
LIST_TTL_SECONDS = 600
DATE_FORMAT = "%d.%m.%Y"


@dataclass(frozen=True)
class Inactive:
    user: UserDto
    subscription: SubscriptionDto
    since: datetime

    def delete_at(self, settings: InactiveCleanupSettingsDto) -> datetime:
        return self.since + timedelta(days=settings.delete_after_days)

    @property
    def lapse(self) -> int:
        return int(self.since.timestamp())


@dataclass
class CleanupReport:
    candidates: int = 0
    warned: int = 0
    deleted: int = 0
    failed: int = 0


def inactive_since(subscription: SubscriptionDto, now: datetime) -> Optional[datetime]:
    """When the subscription stopped working, or None if it still does.

    A disabled subscription carries no date of its own; its last update is the
    closest thing.
    """
    if subscription.is_unlimited:
        return None
    if subscription.status == SubscriptionStatus.DISABLED:
        return subscription.updated_at or now
    if subscription.status == SubscriptionStatus.EXPIRED or subscription.expire_at < now:
        return subscription.expire_at
    return None


class InactiveProfiles:
    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        settings_dao: SettingsDao,
        remnawave: SRemnawave,
        notifier: Notifier,
        delete_subscription: DeleteSubscription,
    ) -> None:
        self.session = session
        self.redis = redis
        self.user_dao = user_dao
        self.subscription_dao = subscription_dao
        self.settings_dao = settings_dao
        self.remnawave = remnawave
        self.notifier = notifier
        self.delete_subscription = delete_subscription

    async def candidates(self, include_disabled: bool) -> list[Inactive]:
        now = datetime_now()
        statuses = [SubscriptionStatus.EXPIRED, SubscriptionStatus.ACTIVE]
        if include_disabled:
            statuses.append(SubscriptionStatus.DISABLED)
        stmt = (
            select(User.id)
            .join(Subscription, User.current_subscription_id == Subscription.id)
            .where(
                Subscription.status.in_(statuses),
                or_(
                    Subscription.status != SubscriptionStatus.ACTIVE,
                    Subscription.expire_at < now,
                ),
            )
        )
        user_ids = list((await self.session.execute(stmt)).scalars())

        found = []
        for user_id in user_ids:
            user = await self.user_dao.get_by_id(user_id)
            subscription = await self.subscription_dao.get_current(user_id)
            if not user or not subscription:
                continue
            since = inactive_since(subscription, now)
            if since is not None:
                found.append(Inactive(user=user, subscription=subscription, since=since))
        found.sort(key=lambda item: item.since)
        return found

    # ── warnings ────────────────────────────────────────────────────────────

    def _warning_key(self, item: Inactive) -> str:
        return serialize_storage_key(
            InactiveWarningKey(subscription_id=item.subscription.id, lapse=item.lapse)
        )

    async def _warned_at(self, item: Inactive) -> Optional[datetime]:
        raw = await self.redis.get(self._warning_key(item))
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    async def _notify(self, user: UserDto, payload: MessagePayloadDto) -> bool:
        try:
            await self.notifier.notify_user(user, payload=payload)
            return True
        except Exception as e:
            logger.warning(f"Inactive-profile notice to {user.log} failed: {e}")
            return False

    # ── the daily run ───────────────────────────────────────────────────────

    async def run(self, excluded_user_ids: Optional[set[int]] = None) -> CleanupReport:
        settings = (await self.settings_dao.get()).extra.s.inactive
        report = CleanupReport()
        if not settings.enabled:
            return report

        now = datetime_now()
        excluded = excluded_user_ids or set()
        items = [
            i for i in await self.candidates(settings.include_disabled) if i.user.id not in excluded
        ]
        report.candidates = len(items)
        warn_for = timedelta(days=settings.warn_days)

        for item in items:
            delete_at = item.delete_at(settings)
            warned_at = await self._warned_at(item)

            if settings.warn_days > 0 and warned_at is None:
                if now >= delete_at - warn_for:
                    await self._warn(item, max(delete_at, now + warn_for), settings)
                    report.warned += 1
                continue

            ready = now >= delete_at and (warned_at is None or now >= warned_at + warn_for)
            if not ready:
                continue
            if await self._delete(item):
                report.deleted += 1
            else:
                report.failed += 1

        logger.info(f"Inactive profiles cleanup: {report}")
        return report

    async def _warn(
        self, item: Inactive, delete_on: datetime, settings: InactiveCleanupSettingsDto
    ) -> None:
        await self._notify(
            item.user,
            MessagePayloadDto(
                i18n_key="ntf-s-inactive.warning",
                i18n_kwargs={"date": delete_on.strftime(DATE_FORMAT), "value": settings.warn_days},
                reply_markup=get_renew_keyboard(),
                disable_default_markup=False,
                delete_after=None,
            ),
        )
        # Kept well past the deletion date so a slow run still finds it.
        ttl = int(timedelta(days=settings.warn_days + 30).total_seconds())
        await self.redis.set(self._warning_key(item), datetime_now().isoformat(), ex=ttl)
        logger.info(f"Inactive profile {item.user.log} warned, deletion on {delete_on.date()}")

    async def _delete(self, item: Inactive) -> bool:
        try:
            await self.delete_subscription.system(item.user.id)
        except Exception as e:
            logger.warning(f"Inactive profile {item.user.log} not deleted: {e}")
            return False
        await self.redis.delete(self._warning_key(item))
        logger.warning(f"Inactive profile {item.user.log} deleted after lapse {item.since}")
        await self._notify(
            item.user,
            MessagePayloadDto(
                i18n_key="ntf-s-inactive.deleted",
                reply_markup=get_buy_keyboard(),
                disable_default_markup=False,
                delete_after=None,
            ),
        )
        return True

    # ── what the user is told up front ──────────────────────────────────────

    @on_event(SubscriptionExpiredEvent)
    async def on_expired(self, event: SubscriptionExpiredEvent) -> None:
        """At expiry, say when the profile goes if nothing is done."""
        settings = (await self.settings_dao.get()).extra.s.inactive
        if not settings.enabled:
            return
        user = event.user
        subscription = await self.subscription_dao.get_current(user.id)
        if not subscription:
            return
        since = inactive_since(subscription, datetime_now()) or datetime_now()
        delete_on = since + timedelta(days=settings.delete_after_days)
        await self._notify(
            user,
            MessagePayloadDto(
                i18n_key="ntf-s-inactive.policy",
                i18n_kwargs={
                    "date": delete_on.strftime(DATE_FORMAT),
                    "value": settings.delete_after_days,
                },
                disable_default_markup=False,
                delete_after=None,
            ),
        )

    # ── the admin list ──────────────────────────────────────────────────────

    async def listing(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        """Inactive profiles, the longest offline first, with the panel's last online.

        Reading the last online is a panel call per profile, so the result is
        kept for a few minutes.
        """
        key = serialize_storage_key(InactiveListKey())
        cached = await self.redis.get(key)
        if cached:
            try:
                return list(json.loads(cached))
            except ValueError:
                pass

        items = await self.candidates(include_disabled)
        semaphore = asyncio.Semaphore(PANEL_CONCURRENCY)

        async def online(item: Inactive) -> Optional[datetime]:
            async with semaphore:
                try:
                    remna_user = await self.remnawave.get_user_by_uuid(
                        item.subscription.user_remna_id
                    )
                except Exception:
                    return None
            return getattr(remna_user, "online_at", None) if remna_user else None

        seen = await asyncio.gather(*(online(item) for item in items))
        rows: list[dict[str, Any]] = [
            {
                "user_id": item.user.id,
                "telegram_id": item.user.telegram_id,
                "name": item.user.name,
                "status": str(item.subscription.status),
                "since": item.since.strftime(DATE_FORMAT),
                "online": at.strftime(DATE_FORMAT) if at else "none",
                "online_ts": at.timestamp() if at else 0,
            }
            for item, at in zip(items, seen, strict=True)
        ]
        rows.sort(key=lambda row: row["online_ts"])
        await self.redis.set(key, json.dumps(rows), ex=LIST_TTL_SECONDS)
        return rows
