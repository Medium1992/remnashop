from contextvars import ContextVar
from typing import Optional
from uuid import UUID

from dishka import AsyncContainer
from loguru import logger
from redis.asyncio import Redis
from remnapy import RemnawaveSDK
from remnapy.enums import UserStatus
from remnapy.models import CreateUserRequestDto, UpdateUserRequestDto, UserResponseDto

from src.application.common.dao import SettingsDao
from src.application.dto import PlanSnapshotDto, SubscriptionDto, UserDto
from src.application.dto.s_settings import SlotsSettingsDto, SSettingsDto
from src.core.types import RemnawaveHttpClient
from src.core.utils.s_calendar import add_term
from src.core.utils.time import datetime_now
from src.infrastructure.database.dao.s_blocks import UserBlockDao
from src.infrastructure.services.remnawave import RemnawaveImpl

from .slot_grants import physical_limit, read_grant

# The device limit to write instead of the plan's, for the call in progress.
# A ContextVar and not an attribute: this object is shared by every request.
# _OMIT leaves the panel's limit untouched.
_OMIT = -1
_device_limit: ContextVar[Optional[int]] = ContextVar("s_device_limit", default=None)
# True while writing a user who is under a temporary block: they stay DISABLED.
_blocked: ContextVar[bool] = ContextVar("s_blocked", default=False)
# True while a plan's term is to be counted in calendar months.
_calendar: ContextVar[bool] = ContextVar("s_calendar", default=False)


class SRemnawave(RemnawaveImpl):
    """The panel client of the "s" edition.

    With strict slots on, creating or updating a user writes the *physical*
    device limit -- what is occupied, plus an open temporary slot -- instead
    of the plan's, which stays in RemnaShop as the ceiling. A user under a
    temporary block is written DISABLED whatever the caller asked for, so a
    plan change during the block does not lift it. With calendar months on, a
    user written from a plan gets an expiry counted by the calendar.
    """

    def __init__(
        self,
        sdk: RemnawaveSDK,
        http: RemnawaveHttpClient,
        container: AsyncContainer,
        redis: Redis,
    ) -> None:
        super().__init__(sdk, http)
        self._container = container
        self._redis = redis

    async def _s(self) -> SSettingsDto:
        async with self._container() as request:
            settings_dao: SettingsDao = await request.get(SettingsDao)
            settings = await settings_dao.get()
            return settings.extra.s

    async def _slots(self) -> SlotsSettingsDto:
        return (await self._s()).slots

    async def _calendar_for(self, plan: Optional[PlanSnapshotDto]) -> bool:
        # Only a plan sets the term; a subscription carries its own expiry.
        return plan is not None and (await self._s()).plans.calendar_months

    async def _is_blocked(self, user_id: int) -> bool:
        if not user_id:
            return False
        async with self._container() as request:
            block_dao: UserBlockDao = await request.get(UserBlockDao)
            return await block_dao.get(user_id) is not None

    async def set_device_limit(self, uuid: UUID, limit: int) -> None:
        panel_id = await self._panel_id_or_raise(uuid)
        await self._panel.update_user(panel_id, UpdateUserRequestDto(hwid_device_limit=limit))
        logger.debug(f"RemnaUser id '{panel_id}' device limit set to '{limit}'")

    async def _strict_limit(self, uuid: Optional[UUID], ceiling: int) -> Optional[int]:
        """The limit strict slots want, None when they do not apply."""
        slots = await self._slots()
        if not slots.enabled or ceiling <= 0:
            return None
        if uuid is None:
            return physical_limit(ceiling, 0, slots.base_limit)
        try:
            occupied = len(await self.get_devices(uuid))
        except Exception as e:
            # Better to leave the panel's limit as it is than to open the full
            # ceiling, or to cut it on a guess.
            logger.warning(f"Device limit of '{uuid}' left as is, devices unavailable: {e}")
            return _OMIT
        grant = await read_grant(self._redis, uuid)
        granted = grant.granted_limit if grant and grant.is_open else None
        return physical_limit(ceiling, occupied, slots.base_limit, granted)

    async def create_user(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
    ) -> UserResponseDto:
        source = subscription or plan
        limit = await self._strict_limit(None, source.device_limit) if source else None
        token = _device_limit.set(limit)
        blocked = _blocked.set(await self._is_blocked(user.id))
        calendar = _calendar.set(subscription is None and await self._calendar_for(plan))
        try:
            return await super().create_user(user, plan, subscription)
        finally:
            _device_limit.reset(token)
            _blocked.reset(blocked)
            _calendar.reset(calendar)

    async def update_user(
        self,
        user: UserDto,
        uuid: UUID,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
        reset_traffic: bool = False,
    ) -> UserResponseDto:
        source = subscription or plan
        limit = await self._strict_limit(uuid, source.device_limit) if source else None
        token = _device_limit.set(limit)
        blocked = _blocked.set(await self._is_blocked(user.id))
        calendar = _calendar.set(subscription is None and await self._calendar_for(plan))
        try:
            return await super().update_user(user, uuid, plan, subscription, reset_traffic)
        finally:
            _device_limit.reset(token)
            _blocked.reset(blocked)
            _calendar.reset(calendar)

    def _build_create_request(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> CreateUserRequestDto:
        request = super()._build_create_request(user, plan, subscription)
        limit = _device_limit.get()
        if limit is not None and limit != _OMIT:
            request.hwid_device_limit = limit
        if _blocked.get():
            request.status = UserStatus.DISABLED
        if plan and _calendar.get() and plan.duration:
            request.expire_at = add_term(datetime_now(), plan.duration)
        return request

    def _build_update_request(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> UpdateUserRequestDto:
        request = super()._build_update_request(user, plan, subscription)
        limit = _device_limit.get()
        if limit == _OMIT:
            request.hwid_device_limit = None
        elif limit is not None:
            request.hwid_device_limit = limit
        if _blocked.get():
            request.status = UserStatus.DISABLED
        if plan and not subscription and _calendar.get() and plan.duration:
            request.expire_at = add_term(datetime_now(), plan.duration)
        return request
