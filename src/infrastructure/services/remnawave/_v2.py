"""Panel 2.7.x-2.8.x implementation: user routes still take a uuid.

RemnaShop stores the numeric panel id as the identity, because that is the only
one that survives a panel upgrade to 3.x. A 2.x panel, though, still addresses a
user by uuid on every action route, so this client translates id -> uuid.

The translation is one extra GET on a cache miss, against ``by-id/{id}`` which
exists in 2.x precisely for this. The mapping never changes for a given user on
a given panel, so it is memoised for the life of the process. Reads that the SDK
can already express (the user itself, listing, by-username, by-short-uuid) do
not need the translation at all.

The requests are issued against the shared httpx client rather than through
remnapy, because remnapy 3.2.1 only knows the 3.x routes. Responses are parsed
with remnapy's own models -- they accept a 2.x payload, whose only extra field
is the ``uuid`` that 3.x dropped -- and errors are raised through remnapy's own
handler, so both lines fail identically upstream.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from httpx import AsyncClient, Response
from loguru import logger
from remnapy.enums import ErrorCode
from remnapy.exceptions import ApiErrorResponse, NotFoundError, handle_api_error
from remnapy.models import UpdateUserRequestDto, UserResponseDto
from remnapy.models.hwid import HwidDeviceDto

from src.core.utils.time import datetime_now

from ._client import update_body

# One entry per user touched by this process; the mapping is immutable for a
# given panel, so entries never need invalidating, only bounding.
_UUID_CACHE_LIMIT = 20_000


class PanelClientV2:
    def __init__(self, http: AsyncClient) -> None:
        self.http = http
        self._uuid_by_id: Dict[int, UUID] = {}

    # -- request plumbing -------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Optional[Any]:
        """Issue a call and return its ``response`` payload, or None on 404."""
        response: Response = await self.http.request(method, path, **kwargs)

        if response.status_code == 404:
            return None

        handle_api_error(response)
        response.raise_for_status()

        if not response.content:
            return None

        return response.json().get("response")

    # -- identity ---------------------------------------------------------

    async def get_user_uuid(self, panel_id: int) -> Optional[UUID]:
        cached = self._uuid_by_id.get(panel_id)
        if cached is not None:
            return cached

        payload = await self._request("GET", f"/users/by-id/{panel_id}")
        if payload is None:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return None

        return self._remember_uuid(panel_id, payload)

    def _remember_uuid(self, panel_id: int, payload: Dict[str, Any]) -> Optional[UUID]:
        raw = payload.get("uuid")
        if not raw:
            # A 2.x panel always sends it; if it ever does not, fail loudly here
            # rather than somewhere further along with a confusing error.
            logger.error(f"Panel returned no uuid for RemnaUser id '{panel_id}'")
            return None

        user_uuid = UUID(str(raw))

        if len(self._uuid_by_id) >= _UUID_CACHE_LIMIT:
            self._uuid_by_id.clear()

        self._uuid_by_id[panel_id] = user_uuid
        return user_uuid

    async def _uuid_or_raise(self, panel_id: int) -> UUID:
        user_uuid = await self.get_user_uuid(panel_id)
        if user_uuid is None:
            raise NotFoundError(404, _not_found(f"users/by-id/{panel_id}"))
        return user_uuid

    # -- user -------------------------------------------------------------

    async def get_user(self, panel_id: int) -> Optional[UserResponseDto]:
        payload = await self._request("GET", f"/users/by-id/{panel_id}")
        if payload is None:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return None

        self._remember_uuid(panel_id, payload)
        return UserResponseDto.model_validate(payload)

    async def update_user(
        self,
        panel_id: int,
        request: UpdateUserRequestDto,
    ) -> UserResponseDto:
        user_uuid = await self._uuid_or_raise(panel_id)

        # The two lines send an identical field set; only the addressing key
        # differs, and 2.x names it uuid.
        body = update_body(request)
        body["uuid"] = str(user_uuid)

        payload = await self._request("PATCH", "/users", json=body)
        if payload is None:
            raise NotFoundError(404, _not_found(f"users/{user_uuid}"))

        self._remember_uuid(panel_id, payload)
        return UserResponseDto.model_validate(payload)

    async def delete_user(self, panel_id: int) -> bool:
        user_uuid = await self.get_user_uuid(panel_id)
        if user_uuid is None:
            return False

        payload = await self._request("DELETE", f"/users/{user_uuid}")
        if payload is None:
            return False

        self._uuid_by_id.pop(panel_id, None)
        return bool(payload.get("isDeleted"))

    async def enable_user(self, panel_id: int) -> None:
        user_uuid = await self._uuid_or_raise(panel_id)
        await self._request("POST", f"/users/{user_uuid}/actions/enable")

    async def disable_user(self, panel_id: int) -> None:
        user_uuid = await self._uuid_or_raise(panel_id)
        await self._request("POST", f"/users/{user_uuid}/actions/disable")

    async def reset_traffic(self, panel_id: int) -> Optional[UserResponseDto]:
        user_uuid = await self.get_user_uuid(panel_id)
        if user_uuid is None:
            return None

        payload = await self._request("POST", f"/users/{user_uuid}/actions/reset-traffic")
        if payload is None:
            return None

        return UserResponseDto.model_validate(payload)

    async def revoke_subscription(self, panel_id: int) -> None:
        user_uuid = await self.get_user_uuid(panel_id)
        if user_uuid is None:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return
        await self._request("POST", f"/users/{user_uuid}/actions/revoke")

    # -- lookups the 3.x panel dropped -------------------------------------

    async def find_users_by_telegram_id(self, telegram_id: int) -> List[UserResponseDto]:
        return await self._user_list("GET", f"/users/by-telegram-id/{telegram_id}")

    async def find_users_by_email(self, email: str) -> List[UserResponseDto]:
        return await self._user_list("GET", f"/users/by-email/{email}")

    async def _user_list(self, method: str, path: str) -> List[UserResponseDto]:
        payload = await self._request(method, path)
        if not payload:
            return []

        users = [UserResponseDto.model_validate(item) for item in payload]
        for item, user in zip(payload, users):
            if item.get("uuid"):
                self._remember_uuid(user.id, item)

        return users

    # -- devices -----------------------------------------------------------

    async def get_devices(self, panel_id: int) -> List[HwidDeviceDto]:
        user_uuid = await self.get_user_uuid(panel_id)
        if user_uuid is None:
            return []

        payload = await self._request("GET", f"/hwid/devices/{user_uuid}")
        if not payload or not payload.get("total"):
            return []

        return [self._as_device(d, panel_id) for d in payload.get("devices", [])]

    @staticmethod
    def _as_device(raw: Dict[str, Any], panel_id: int) -> HwidDeviceDto:
        # A 2.x panel labels the owner ``userUuid``; the device model wants the
        # numeric ``userId`` that 3.x switched to. The owner is known already,
        # so fill it in rather than carrying a second device model.
        return HwidDeviceDto.model_validate({**raw, "userId": panel_id})

    async def delete_device(self, panel_id: int, hwid: str) -> Optional[int]:
        user_uuid = await self.get_user_uuid(panel_id)
        if user_uuid is None:
            return None

        payload = await self._request(
            "POST",
            "/hwid/devices/delete",
            json={"userUuid": str(user_uuid), "hwid": hwid},
        )
        if payload is None:
            return None

        return int(payload.get("total", 0))

    async def delete_all_devices(self, panel_id: int) -> None:
        user_uuid = await self.get_user_uuid(panel_id)
        if user_uuid is None:
            logger.debug(f"RemnaUser id '{panel_id}' not found in panel")
            return

        await self._request(
            "POST",
            "/hwid/devices/delete-all",
            json={"userUuid": str(user_uuid)},
        )

    async def drop_connections(self, panel_id: int) -> None:
        user_uuid = await self._uuid_or_raise(panel_id)
        await self._request(
            "POST",
            "/ip-control/drop-connections",
            json={
                "dropBy": {"by": "userUuids", "userUuids": [str(user_uuid)]},
                "targetNodes": {"target": "allNodes"},
            },
        )


def _not_found(path: str) -> ApiErrorResponse:
    """Shape a 404 the way remnapy's own error handler would have."""
    return ApiErrorResponse(
        timestamp=datetime_now(),
        path=path,
        message="User not found",
        code=ErrorCode.USER_NOT_FOUND,
        status_code=404,
    )
