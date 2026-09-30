"""The operations whose HTTP shape differs between the two panel contracts.

Everything RemnaShop asks of the panel is identical across 2.7.x and 3.4.x
except the calls collected here. They are the ones that address a *user*: the
panel dropped ``users.uuid`` in 3.0.0 and re-keyed every user-addressing route
and body onto the numeric id, and it renamed the ip-control controller to
connections at the same time.

The protocol below is therefore written entirely in terms of the numeric panel
id, which is the identity that exists in both lines. The 2.x implementation
translates it back to a uuid where the older routes still demand one; the 3.x
implementation passes it straight through.

Calls that did *not* change -- creating and updating a user (both accept
``username``, which works in either line), listing users, looking a user up by
username or short uuid, squads, nodes, hosts, system metadata and stats -- are
not here. They go to the SDK directly, verified against the panel's contract
history and against a captured wire trace.
"""

from typing import Any, Dict, Final, FrozenSet, List, Optional, Protocol
from uuid import UUID

from remnapy.models import UpdateUserRequestDto, UserResponseDto
from remnapy.models.hwid import HwidDeviceDto

# Fields where passing nothing and passing null mean different things, and where
# RemnaShop means the second one: null detaches the user from the value.
#
# remnapy serialises bodies with exclude_none, which silently turns "detach" into
# "leave alone" -- moving a user onto a plan without an external squad, or onto
# one with no tag, would leave the old value in place forever. These are re-added
# explicitly.
#
# Clearing the tag also drops the IMPORTED marker the importer sets, but only for
# a user who has since moved onto a real plan, by which point the marker is no
# longer true. It gates adoption on the user.created webhook only, so an
# already-adopted user is unaffected, and an imported subscription carries
# IMPORTED in its own plan snapshot and keeps re-sending it.
#
# Only fields the panel declares nullable in *both* lines may appear here; the
# rest (expireAt, status, trafficLimit*, activeInternalSquads) are optional but
# not nullable, and sending null for them fails validation.
CLEARABLE_FIELDS: Final[FrozenSet[str]] = frozenset({"external_squad_uuid", "tag"})


def update_body(request: UpdateUserRequestDto) -> Dict[str, Any]:
    """Serialise an update the way remnapy would, but honouring explicit nulls.

    Excludes the addressing keys: each panel client adds the one its line uses.
    """
    body = request.model_dump(mode="json", exclude_none=True, by_alias=True)

    for name in CLEARABLE_FIELDS:
        # Set-to-None is a deliberate detach; never-set stays absent.
        if name in request.model_fields_set and getattr(request, name) is None:
            field = type(request).model_fields[name]
            body[field.serialization_alias or field.alias or name] = None

    body.pop("id", None)
    body.pop("username", None)
    return body


class PanelClient(Protocol):
    """User-addressing calls, keyed by the panel's numeric user id."""

    async def get_user(self, panel_id: int) -> Optional[UserResponseDto]: ...

    async def update_user(
        self,
        panel_id: int,
        request: UpdateUserRequestDto,
    ) -> UserResponseDto: ...
    """Apply an update to one user.

    Addressed by id rather than by username on purpose: an imported user's panel
    username is whatever it was before RemnaShop adopted them, so it is not a
    reliable way to name the account being changed.
    """

    async def delete_user(self, panel_id: int) -> bool: ...

    async def enable_user(self, panel_id: int) -> None: ...

    async def disable_user(self, panel_id: int) -> None: ...

    async def reset_traffic(self, panel_id: int) -> Optional[UserResponseDto]: ...

    async def revoke_subscription(self, panel_id: int) -> None: ...

    async def find_users_by_telegram_id(self, telegram_id: int) -> List[UserResponseDto]: ...

    async def find_users_by_email(self, email: str) -> List[UserResponseDto]: ...

    async def get_devices(self, panel_id: int) -> List[HwidDeviceDto]: ...

    async def delete_device(self, panel_id: int, hwid: str) -> Optional[int]: ...

    async def delete_all_devices(self, panel_id: int) -> None: ...

    async def drop_connections(self, panel_id: int) -> None: ...

    async def get_user_uuid(self, panel_id: int) -> Optional[UUID]: ...
    """The panel's own uuid for a user, or None on 3.x where none exists.

    Only meaningful for callers that must show or log the legacy identifier.
    """
