"""Turning the stored ``user_remna_id`` into the panel id a request needs.

Rows written by this build already hold the encoded numeric id, so the common
path is a pure decode with no network call. Rows written by an earlier RemnaShop
hold a real 2.x panel uuid instead, and have to be resolved once:

* against a 2.x panel the uuid still works, so the id comes back from a single
  ``GET /users/{uuid}``;
* against a 3.x panel the uuid no longer exists anywhere -- the panel's own
  migration dropped the column -- so the only way back is an identifier that
  survived the upgrade. ``shortUuid`` did, and RemnaShop already stores it
  inside ``subscriptions.url``; the username is the second try, and it is the
  weaker one because imported users carry arbitrary names.

Resolution is normally done up front by the startup backfill, which rewrites the
rows. This resolver is what that backfill uses, and what covers a row it could
not reach at the time.
"""

import re
from typing import Final, Optional
from uuid import UUID

from httpx import AsyncClient
from loguru import logger
from remnapy import RemnawaveSDK
from remnapy.exceptions import ApiError

from src.core.utils.remna_id import decode_remna_id

# The subscription url ends in the panel's shortUuid, optionally followed by a
# template suffix the panel appends for specific clients.
_SHORT_UUID_RE: Final[re.Pattern[str]] = re.compile(r"/([A-Za-z0-9_-]{16,64})/?$")


def short_uuid_from_url(url: Optional[str]) -> Optional[str]:
    """Pull the panel's shortUuid out of a stored subscription url."""
    if not url:
        return None

    match = _SHORT_UUID_RE.search(url.split("?")[0].rstrip("/"))
    return match.group(1) if match else None


class RemnaIdentity:
    """Resolves a stored ``user_remna_id`` to the panel's numeric user id."""

    def __init__(self, sdk: RemnawaveSDK, http: AsyncClient, is_v3: bool) -> None:
        self.sdk = sdk
        self.http = http
        self.is_v3 = is_v3

    async def resolve(
        self,
        stored: UUID,
        *,
        subscription_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[int]:
        panel_id = decode_remna_id(stored)
        if panel_id is not None:
            return panel_id

        return await self.resolve_legacy(
            stored,
            subscription_url=subscription_url,
            username=username,
        )

    async def resolve_legacy(
        self,
        stored: UUID,
        *,
        subscription_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[int]:
        """Find the panel id behind a real 2.x uuid written by an older build."""
        if not self.is_v3:
            panel_id = await self._by_legacy_uuid(stored)
            if panel_id is not None:
                return panel_id

        short_uuid = short_uuid_from_url(subscription_url)
        if short_uuid:
            panel_id = await self._by_short_uuid(short_uuid)
            if panel_id is not None:
                return panel_id

        if username:
            panel_id = await self._by_username(username)
            if panel_id is not None:
                return panel_id

        logger.warning(
            f"Could not resolve legacy RemnaUser '{stored}' to a panel id "
            f"(short_uuid='{short_uuid}', username='{username}')"
        )
        return None

    async def _by_legacy_uuid(self, stored: UUID) -> Optional[int]:
        # Only a 2.x panel still answers this; it is skipped entirely on 3.x.
        try:
            response = await self.http.get(f"/users/{stored}")
        except Exception as e:
            logger.debug(f"Legacy uuid lookup for '{stored}' failed: {e}")
            return None

        if response.status_code != 200:
            return None

        payload = response.json().get("response") or {}
        panel_id = payload.get("id")
        return int(panel_id) if panel_id is not None else None

    async def _by_short_uuid(self, short_uuid: str) -> Optional[int]:
        try:
            user = await self.sdk.users.get_user_by_short_uuid(short_uuid)
        except ApiError:
            return None
        return user.id

    async def _by_username(self, username: str) -> Optional[int]:
        try:
            user = await self.sdk.users.get_user_by_username(username)
        except ApiError:
            return None
        return user.id
