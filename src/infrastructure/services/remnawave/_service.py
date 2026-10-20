"""The Remnawave port, implemented once over both panel contracts.

The line is decided once, on connect, from ``GET /system/metadata`` -- the one
endpoint that never changed across the supported range. From then on the
version-specific calls go to the client for that line and everything else goes
straight to the SDK, whose models parse either line's payloads.

Callers keep passing the value stored in ``subscriptions.user_remna_id``. Here it
is decoded into the panel's numeric user id; rows written by an older RemnaShop
still hold a real 2.x uuid and are resolved on the spot, which the startup
backfill normally has already done.
"""

import asyncio
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Union
from uuid import UUID

from loguru import logger
from packaging.version import Version
from remnapy import RemnawaveSDK
from remnapy.enums import ErrorCode
from remnapy.exceptions import (
    ApiErrorResponse,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    handle_api_error,
)
from remnapy.models import (
    CreateUserRequestDto,
    GetExternalSquadByUuidResponseDto,
    GetMetadataResponseDto,
    HostResponseDto,
    NodeResponseDto,
    UpdateUserRequestDto,
    UserResponseDto,
)
from remnapy.models.hwid import HwidDeviceDto

from src.application.common import Remnawave
from src.application.common.remnawave import T
from src.application.dto import (
    PlanSnapshotDto,
    RemnaSubscriptionDto,
    SquadInfoDto,
    SubscriptionDto,
    UserDto,
)
from src.core.enums import SubscriptionStatus
from src.core.panel import MIN_PANEL_VERSION, PanelLine, is_supported
from src.core.types import RemnawaveHttpClient
from src.core.utils.converters import days_to_datetime, gb_to_bytes
from src.core.utils.remna_id import decode_remna_id
from src.core.utils.time import datetime_now

from ._compat import normalise_hosts, normalise_node, normalise_nodes
from ._identity import RemnaIdentity
from ._v2 import PanelClientV2
from ._v3 import PanelClientV3


class RemnawaveImpl(Remnawave):
    def __init__(self, sdk: RemnawaveSDK, http: RemnawaveHttpClient) -> None:
        self.sdk = sdk
        self.http = http

        # Until the panel answers, assume the older contract: it addresses users
        # by uuid, which is also what any pre-existing stored row holds, so a
        # call made before the handshake degrades to a lookup rather than to a
        # request against a route that may not exist.
        self._line = PanelLine.V2
        self._panel = self._build_client(PanelLine.V2)
        self._identity = RemnaIdentity(sdk, http, is_v3=False)
        self._detected = False
        self._detect_lock = asyncio.Lock()

    @property
    def line(self) -> PanelLine:
        return self._line

    def _build_client(self, line: PanelLine) -> Union[PanelClientV2, PanelClientV3]:
        return PanelClientV3(self.sdk, self.http) if line.is_v3 else PanelClientV2(self.http)

    def _adopt(self, version: Version) -> None:
        self._line = PanelLine.of(version)
        self._panel = self._build_client(self._line)
        self._identity = RemnaIdentity(self.sdk, self.http, is_v3=self._line.is_v3)
        self._detected = True

    async def _ensure_detected(self) -> None:
        """Settle which contract the panel speaks, if startup could not.

        The handshake normally happens once, on connect. It can fail there --
        the panel is often still starting when the bot is, especially right
        after a panel upgrade -- and the fallback line is the older contract.
        Left at that, every user-addressing call against a 3.x panel would go to
        a route that no longer exists, until someone restarted the bot.

        A single metadata call is enough to settle it, and it is only ever made
        while the line is still unknown.
        """
        if self._detected:
            return

        # The service is a singleton, so a burst of requests arriving before the
        # panel answered would otherwise each fire their own handshake.
        async with self._detect_lock:
            if self._detected:
                return

            try:
                metadata = await self.sdk.system.get_metadata()
            except Exception as e:
                logger.debug(f"Panel contract still undetermined: {e}")
                return

            version = Version(metadata.version)
            self._adopt(version)

        logger.info(
            f"Determined Remnawave API contract on first use "
            f"(version: {version}, contract: {self._line.value})"
        )

    # -- connection --------------------------------------------------------

    async def try_connection(self) -> Version:
        metadata = await self._fetch_metadata()

        if not isinstance(metadata, GetMetadataResponseDto):
            logger.error(f"Invalid response from Remnawave panel: '{metadata}'")
            raise ValueError(f"Invalid response from Remnawave panel: {metadata}")

        panel_version = Version(metadata.version)

        if not is_supported(panel_version):
            message = (
                f"Remnawave panel version '{panel_version}' is not compatible. "
                f"Minimum required version: '{MIN_PANEL_VERSION}'"
            )
            logger.error(message)
            raise ValueError(message)

        self._adopt(panel_version)
        logger.info(
            f"Successfully connected to Remnawave panel "
            f"(version: {panel_version}, API contract: {self._line.value})"
        )
        return panel_version

    async def _fetch_metadata(self) -> GetMetadataResponseDto:
        for attempt in range(1, 4):
            try:
                return await self.sdk.system.get_metadata()
            except AuthenticationError as e:
                logger.error(f"Authentication failed when connecting to Remnawave panel: '{e}'")
                raise
            except Exception as e:
                if attempt == 3:
                    logger.error(f"Failed to connect to Remnawave panel after 3 attempts: '{e}'")
                    raise
                logger.warning(
                    f"Failed to connect to Remnawave panel (attempt {attempt}/3): '{e}', "
                    f"retrying in 5s..."
                )
                await asyncio.sleep(5)

        raise AssertionError("unreachable")

    # -- identity ----------------------------------------------------------

    async def _panel_id(self, stored: UUID) -> Optional[int]:
        await self._ensure_detected()

        panel_id = decode_remna_id(stored)
        if panel_id is not None:
            return panel_id

        logger.debug(f"Resolving legacy RemnaUser identity '{stored}'")
        return await self._identity.resolve_legacy(stored)

    async def _panel_id_or_raise(self, stored: UUID) -> int:
        panel_id = await self._panel_id(stored)
        if panel_id is None:
            raise NotFoundError(
                404,
                _missing(f"Cannot resolve RemnaUser '{stored}' to a panel user id"),
            )
        return panel_id

    async def resolve_legacy_identity(
        self,
        stored: UUID,
        subscription_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[int]:
        await self._ensure_detected()

        return await self._identity.resolve_legacy(
            stored,
            subscription_url=subscription_url,
            username=username,
        )

    # -- users -------------------------------------------------------------

    async def create_user(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
    ) -> UserResponseDto:
        request_dto = self._build_create_request(user, plan, subscription)

        try:
            remna_user = await self.sdk.users.create_user(request_dto)
        except ConflictError:
            logger.warning(
                f"RemnaUser '{request_dto.username}' already exists in panel, adopting it"
            )
            return await self._adopt_existing_user(request_dto)

        logger.info(
            f"RemnaUser '{remna_user.username}' created successfully. "
            f"id: '{remna_user.id}', telegram_id: '{remna_user.telegram_id}'"
        )
        return remna_user

    async def _adopt_existing_user(self, request: CreateUserRequestDto) -> UserResponseDto:
        """Take over a panel user that already carries this account's username.

        The username is derived from the account (``rs_<telegram id>``), so a
        panel user holding it belongs to this account. It exists when an earlier
        creation got as far as the panel and then failed before RemnaShop saved
        anything -- which every purchase and trial does when an older RemnaShop
        talks to a 3.x panel: the panel creates the user, and the reply, lacking
        the uuid the old SDK insists on, fails to parse. Without this the
        account could never get a subscription again.

        The user is brought to the state the creation asked for, except that an
        expiry already further out than the requested one is kept: the orphan
        may carry time that was paid for.
        """
        existing = await self.sdk.users.get_user_by_username(request.username)

        expire_at = request.expire_at
        if existing.expire_at and existing.expire_at > expire_at:
            logger.warning(
                f"RemnaUser '{request.username}' already runs until "
                f"'{existing.expire_at}', keeping that over '{expire_at}'"
            )
            expire_at = existing.expire_at

        update = UpdateUserRequestDto(
            status=SubscriptionStatus.ACTIVE,
            expire_at=expire_at,
            telegram_id=request.telegram_id,
            email=request.email,
            description=request.description,
            tag=request.tag,
            traffic_limit_strategy=request.traffic_limit_strategy,
            traffic_limit_bytes=request.traffic_limit_bytes,
            hwid_device_limit=request.hwid_device_limit,
            active_internal_squads=request.active_internal_squads,
            external_squad_uuid=request.external_squad_uuid,
        )

        await self._ensure_detected()
        remna_user = await self._panel.update_user(existing.id, update)

        logger.info(
            f"Adopted existing RemnaUser '{remna_user.username}' "
            f"(id: '{remna_user.id}') instead of creating a duplicate"
        )
        return remna_user

    async def update_user(
        self,
        user: UserDto,
        uuid: UUID,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
        reset_traffic: bool = False,
    ) -> UserResponseDto:
        panel_id = await self._panel_id_or_raise(uuid)
        request_dto = self._build_update_request(user, plan, subscription)

        try:
            remna_user = await self._panel.update_user(panel_id, request_dto)
        except NotFoundError:
            logger.warning(f"RemnaUser id '{panel_id}' not found")
            raise

        logger.info(
            f"RemnaUser '{remna_user.username}' updated successfully. "
            f"id: '{remna_user.id}', telegram_id: '{remna_user.telegram_id}'"
        )

        if reset_traffic:
            await self._panel.reset_traffic(panel_id)

        return remna_user

    async def enable_user(self, uuid: UUID) -> None:
        panel_id = await self._panel_id_or_raise(uuid)
        await self._panel.enable_user(panel_id)
        logger.info(f"RemnaUser id '{panel_id}' enabled successfully")

    async def disable_user(self, uuid: UUID) -> None:
        panel_id = await self._panel_id_or_raise(uuid)
        await self._panel.disable_user(panel_id)
        logger.info(f"RemnaUser id '{panel_id}' disabled successfully")

    async def delete_user(self, uuid: UUID) -> bool:
        panel_id = await self._panel_id(uuid)
        if panel_id is None:
            return False

        deleted = await self._panel.delete_user(panel_id)
        if deleted:
            logger.info(f"RemnaUser id '{panel_id}' deleted successfully")
        else:
            logger.warning(f"Failed to delete RemnaUser id '{panel_id}'")
        return deleted

    async def get_user_by_uuid(self, uuid: UUID) -> Optional[UserResponseDto]:
        panel_id = await self._panel_id(uuid)
        if panel_id is None:
            return None
        return await self._panel.get_user(panel_id)

    async def get_users_by_telegram_id(self, telegram_id: int) -> List[UserResponseDto]:
        await self._ensure_detected()

        users = await self._panel.find_users_by_telegram_id(telegram_id)
        logger.debug(f"Fetched {len(users)} RemnaUsers for telegram_id '{telegram_id}'")
        return users

    async def get_users_by_email(self, email: str) -> List[UserResponseDto]:
        await self._ensure_detected()

        users = await self._panel.find_users_by_email(email)
        logger.debug(f"Fetched {len(users)} RemnaUsers for email '{email}'")
        return users

    # Kept under its original name as well: existing callers use both spellings.
    async def get_user_by_email(self, email: str) -> List[UserResponseDto]:
        return await self.get_users_by_email(email)

    async def get_all_users(self, limit: int, offset: int) -> List[UserResponseDto]:
        response = await self.sdk.users.get_all_users(start=offset, size=limit)
        logger.debug(f"Fetched {len(response.users)} RemnaUsers (limit={limit}, offset={offset})")
        return list(response.users)

    async def reset_traffic(self, uuid: UUID) -> Optional[UserResponseDto]:
        panel_id = await self._panel_id(uuid)
        if panel_id is None:
            return None

        remna_user = await self._panel.reset_traffic(panel_id)
        if remna_user:
            logger.info(f"Traffic for RemnaUser id '{panel_id}' reset successfully")
        return remna_user

    async def revoke_subscription(self, uuid: UUID) -> None:
        panel_id = await self._panel_id(uuid)
        if panel_id is None:
            return
        await self._panel.revoke_subscription(panel_id)
        logger.info(f"Subscription for RemnaUser id '{panel_id}' revoked successfully")

    # -- devices -----------------------------------------------------------

    async def get_devices(self, uuid: UUID) -> List[HwidDeviceDto]:
        panel_id = await self._panel_id(uuid)
        if panel_id is None:
            return []

        devices = await self._panel.get_devices(panel_id)
        logger.debug(f"Fetched {len(devices)} devices for RemnaUser id '{panel_id}'")
        return devices

    async def delete_device(self, user_uuid: UUID, hwid: str) -> Optional[int]:
        panel_id = await self._panel_id(user_uuid)
        if panel_id is None:
            return None

        total = await self._panel.delete_device(panel_id, hwid)
        if total is not None:
            logger.info(
                f"Deleted HWID device '{hwid}' for RemnaUser id '{panel_id}'. "
                f"Total devices now: {total}"
            )
        return total

    async def delete_all_devices(self, user_uuid: UUID) -> None:
        panel_id = await self._panel_id(user_uuid)
        if panel_id is None:
            return

        await self._panel.delete_all_devices(panel_id)
        logger.info(f"Deleted all HWID devices for RemnaUser id '{panel_id}'")

    async def drop_connections(self, user_uuid: UUID) -> None:
        try:
            panel_id = await self._panel_id_or_raise(user_uuid)
            await self._panel.drop_connections(panel_id)
            logger.info(f"Dropped connections for RemnaUser id '{panel_id}'")
        except Exception as e:
            logger.warning(f"Failed to drop connections for RemnaUser '{user_uuid}': {e}")

    # -- panel inventory ---------------------------------------------------

    async def get_nodes(self) -> List[NodeResponseDto]:
        response = await self.http.get("/nodes")
        handle_api_error(response)
        response.raise_for_status()

        raw = response.json().get("response") or []
        return [NodeResponseDto.model_validate(n) for n in normalise_nodes(raw)]

    async def get_node(self, uuid: UUID) -> Optional[NodeResponseDto]:
        response = await self.http.get(f"/nodes/{uuid}")
        if response.status_code == 404:
            return None

        handle_api_error(response)
        response.raise_for_status()

        return NodeResponseDto.model_validate(normalise_node(response.json()["response"]))

    async def get_hosts(self) -> List[HostResponseDto]:
        response = await self.http.get("/hosts")
        handle_api_error(response)
        response.raise_for_status()

        raw = response.json().get("response") or []
        return [HostResponseDto.model_validate(h) for h in normalise_hosts(raw)]

    async def get_external_squad(self, uuid: UUID) -> Optional[GetExternalSquadByUuidResponseDto]:
        try:
            return await self.sdk.external_squads.get_external_squad_by_uuid(uuid=uuid)
        except NotFoundError:
            return None

    # -- squads ------------------------------------------------------------

    async def get_squads_available(self) -> bool:
        result = await self.sdk.internal_squads.get_internal_squads()
        return bool(result.internal_squads)

    async def get_internal_squads(self) -> List[SquadInfoDto]:
        result = await self.sdk.internal_squads.get_internal_squads()
        return [SquadInfoDto(uuid=s.uuid, name=s.name) for s in result.internal_squads]

    async def get_external_squads(self) -> List[SquadInfoDto]:
        result = await self.sdk.external_squads.get_external_squads()
        return [SquadInfoDto(uuid=s.uuid, name=s.name) for s in result.external_squads]

    # -- syncing -----------------------------------------------------------

    def apply_sync(self, target: T, source: Union[SubscriptionDto, RemnaSubscriptionDto]) -> T:
        if not is_dataclass(target) or not is_dataclass(source):
            raise TypeError("Both target and source must be dataclasses")

        target_fields = {f.name for f in fields(target)}
        source_fields = {f.name for f in fields(source)}

        field_map = {"user_remna_id": "uuid"}

        for target_field, source_field in field_map.items():
            if target_field in target_fields and source_field in source_fields:
                old_value = getattr(target, target_field)
                new_value = getattr(source, source_field)

                if old_value != new_value:
                    logger.debug(
                        f"Field '{target_field}' changed from '{old_value}' to '{new_value}'"
                    )
                    setattr(target, target_field, new_value)

        for field_name in target_fields & source_fields:
            old_value = getattr(target, field_name)
            new_value = getattr(source, field_name)

            if old_value != new_value:
                logger.debug(f"Field '{field_name}' changed from '{old_value}' to '{new_value}'")
                setattr(target, field_name, new_value)

        return target

    # -- request builders --------------------------------------------------

    def _build_create_request(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> CreateUserRequestDto:
        if subscription:
            # No uuid is passed any more: 3.x removed the field, and the identity
            # RemnaShop stores is now the id the panel assigns in its reply.
            return CreateUserRequestDto(
                username=user.remna_name,
                telegram_id=user.telegram_id,
                expire_at=subscription.expire_at,
                traffic_limit_strategy=subscription.traffic_limit_strategy,
                traffic_limit_bytes=gb_to_bytes(subscription.traffic_limit),
                hwid_device_limit=subscription.device_limit,
                description=user.remna_description,
                email=user.email,
                tag=subscription.tag,
                active_internal_squads=subscription.internal_squads,
                external_squad_uuid=subscription.external_squad,
            )

        if plan:
            return CreateUserRequestDto(
                username=user.remna_name,
                telegram_id=user.telegram_id,
                expire_at=days_to_datetime(plan.duration),
                traffic_limit_strategy=plan.traffic_limit_strategy,
                traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
                hwid_device_limit=plan.device_limit,
                description=user.remna_description,
                email=user.email,
                tag=plan.tag,
                active_internal_squads=plan.internal_squads,
                external_squad_uuid=plan.external_squad,
            )

        return CreateUserRequestDto(
            username=user.remna_name,
            telegram_id=user.telegram_id,
            expire_at=datetime_now() + timedelta(days=3650),
            description=user.remna_description,
            email=user.email,
        )

    @staticmethod
    def _updatable_expiry(expire_at: datetime) -> Optional[datetime]:
        """The expiry to send, or None to leave the panel's alone.

        Both panel lines refuse an update whose expireAt is already past, and
        they refuse the *whole* request -- so a traffic or device change riding
        along with a lapsed date is lost with it. That happens on ordinary
        paths: reconciling a subscription that has run out, or applying a
        traffic promocode to one.

        Omitting a past date lets the rest of the update land. The user stays
        expired either way, which is what the local record says anyway; a
        renewal always carries a future date and is unaffected.
        """
        if expire_at <= datetime_now():
            logger.debug(
                f"Omitting expire_at '{expire_at}' from the update: the panel "
                f"rejects an expiry in the past"
            )
            return None

        return expire_at

    @staticmethod
    def _updatable_status(
        status: SubscriptionStatus,
        expire_at: Optional[datetime],
    ) -> Optional[SubscriptionStatus]:
        """The status to send, or None to leave the panel's alone.

        A lapsed subscription has had its expiry left out of the request (see
        :meth:`_updatable_expiry`). Marking it ACTIVE regardless would switch
        the user back on against a date that has already passed, and they would
        have access until the panel's own expiry job caught up.
        """
        if status == SubscriptionStatus.DISABLED:
            return SubscriptionStatus.DISABLED
        if expire_at is None:
            return None
        return SubscriptionStatus.ACTIVE

    def _build_update_request(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> UpdateUserRequestDto:
        # The addressing key is filled in by the panel client, which knows
        # whether this line wants an id or a uuid.
        if subscription:
            expire_at = self._updatable_expiry(subscription.expire_at)
            return UpdateUserRequestDto(
                telegram_id=user.telegram_id,
                expire_at=expire_at,
                status=self._updatable_status(subscription.status, expire_at),
                traffic_limit_strategy=subscription.traffic_limit_strategy,
                traffic_limit_bytes=gb_to_bytes(subscription.traffic_limit),
                hwid_device_limit=subscription.device_limit,
                description=user.remna_description,
                email=user.email,
                tag=subscription.tag,
                active_internal_squads=subscription.internal_squads,
                external_squad_uuid=subscription.external_squad,
            )

        if plan:
            return UpdateUserRequestDto(
                telegram_id=user.telegram_id,
                expire_at=days_to_datetime(plan.duration),
                status=SubscriptionStatus.ACTIVE,
                traffic_limit_strategy=plan.traffic_limit_strategy,
                traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
                hwid_device_limit=plan.device_limit,
                description=user.remna_description,
                email=user.email,
                tag=plan.tag,
                active_internal_squads=plan.internal_squads,
                external_squad_uuid=plan.external_squad,
            )

        raise ValueError("Either 'plan' or 'subscription' must be provided")


def _missing(message: str) -> ApiErrorResponse:
    """Shape a 404 the way remnapy's own error handler would have."""
    return ApiErrorResponse(
        message=message,
        code=ErrorCode.USER_NOT_FOUND,
        status_code=404,
    )
