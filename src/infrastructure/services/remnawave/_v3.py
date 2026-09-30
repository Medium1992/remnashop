"""Panel 3.0.0+ implementation: the numeric id is the panel's own key.

Every route here is what remnapy 3.2.1 already speaks, so this is a thin pass
through. Verified against the panel contract from 3.0.0 to 3.4.2: for the calls
RemnaShop makes, that range is a single unchanged contract.

The two places that still need care are the user lookups the panel deleted in
3.0.0 (``by-telegram-id`` and ``by-email``), which are now expressed as filters
on the user list. Those filters match with SQL ``LIKE`` on the panel side, so
the results are narrowed again here before anyone sees them.
"""

from typing import List, Optional
from uuid import UUID

from httpx import AsyncClient
from loguru import logger
from remnapy import RemnawaveSDK
from remnapy.exceptions import NotFoundError, handle_api_error
from remnapy.models import (
    DeleteUserAllHwidDeviceRequestDto,
    DeleteUserHwidDeviceRequestDto,
    DropByUserIds,
    DropConnectionsRequestDto,
    TargetAllNodes,
    UpdateUserRequestDto,
    UserResponseDto,
)
from remnapy.models.hwid import HwidDeviceDto
from remnapy.models.table_query import TableFilter

from ._client import update_body

# The panel caps the user list at 1000 per request.
_FILTER_PAGE_SIZE = 1000


class PanelClientV3:
    def __init__(self, sdk: RemnawaveSDK, http: AsyncClient) -> None:
        self.sdk = sdk
        self.http = http

    async def get_user(self, panel_id: int) -> Optional[UserResponseDto]:
        try:
            return await self.sdk.users.get_user_by_id(panel_id)
        except NotFoundError:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return None

    async def update_user(
        self,
        panel_id: int,
        request: UpdateUserRequestDto,
    ) -> UserResponseDto:
        # Sent raw rather than through the SDK so that a deliberate null -- an
        # external squad being detached -- survives serialisation. The route and
        # field names are identical either way; only the addressing key differs,
        # and 3.x names it id.
        body = update_body(request)
        body["id"] = panel_id

        response = await self.http.patch("/users", json=body)
        handle_api_error(response)
        response.raise_for_status()

        return UserResponseDto.model_validate(response.json()["response"])

    async def delete_user(self, panel_id: int) -> bool:
        # 3.x answers a deletion with no body at all -- the ``isDeleted`` flag
        # 2.x returned was dropped -- so success is the absence of an error.
        try:
            await self.sdk.users.delete_user(panel_id)
        except NotFoundError:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return False
        return True

    async def enable_user(self, panel_id: int) -> None:
        await self.sdk.users.enable_user(panel_id)

    async def disable_user(self, panel_id: int) -> None:
        await self.sdk.users.disable_user(panel_id)

    async def reset_traffic(self, panel_id: int) -> Optional[UserResponseDto]:
        try:
            return await self.sdk.users.reset_user_traffic(panel_id)
        except NotFoundError:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return None

    async def revoke_subscription(self, panel_id: int) -> None:
        try:
            await self.sdk.users.revoke_user_subscription(panel_id)
        except NotFoundError:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")

    async def find_users_by_telegram_id(self, telegram_id: int) -> List[UserResponseDto]:
        found = await self._filter_users("telegramId", str(telegram_id))
        # The panel matches telegramId with LIKE '%value%' over a text cast, so
        # a search for 12345 also returns 912345678. Narrowing to an exact match
        # here is what keeps one user's subscription from resolving to another's.
        return [user for user in found if user.telegram_id == telegram_id]

    async def find_users_by_email(self, email: str) -> List[UserResponseDto]:
        found = await self._filter_users("email", email)
        # Same substring caveat as telegramId, plus email is case-insensitive.
        wanted = email.casefold()
        return [user for user in found if (user.email or "").casefold() == wanted]

    async def _filter_users(self, column: str, value: str) -> List[UserResponseDto]:
        response = await self.sdk.users.get_all_users(
            start=0,
            size=_FILTER_PAGE_SIZE,
            filters=[TableFilter(id=column, value=value)],
            filter_modes={column: "contains"},
        )
        return list(response.users)

    async def get_devices(self, panel_id: int) -> List[HwidDeviceDto]:
        response = await self.sdk.hwid.get_hwid_user(panel_id)
        return list(response.devices) if response.total else []

    async def delete_device(self, panel_id: int, hwid: str) -> Optional[int]:
        try:
            response = await self.sdk.hwid.delete_hwid_to_user(
                DeleteUserHwidDeviceRequestDto(user_id=panel_id, hwid=hwid)
            )
        except NotFoundError:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return None
        return int(response.total)

    async def delete_all_devices(self, panel_id: int) -> None:
        try:
            await self.sdk.hwid.delete_all_hwid_user(
                DeleteUserAllHwidDeviceRequestDto(user_id=panel_id)
            )
        except NotFoundError:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")

    async def drop_connections(self, panel_id: int) -> None:
        await self.sdk.connections.drop_connections(
            body=DropConnectionsRequestDto(
                drop_by=DropByUserIds(user_ids=[panel_id]),
                target_nodes=TargetAllNodes(),
            )
        )

    async def get_user_uuid(self, panel_id: int) -> Optional[UUID]:
        # A 3.x panel has no per-user uuid at all: the column was dropped.
        return None
