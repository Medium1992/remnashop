from typing import List, Optional, Protocol, TypeVar, Union
from uuid import UUID

from packaging.version import Version
from remnapy.models import (
    GetExternalSquadByUuidResponseDto,
    HostResponseDto,
    NodeResponseDto,
    UserResponseDto,
)
from remnapy.models.hwid import HwidDeviceDto

from src.application.dto import (
    PlanSnapshotDto,
    RemnaSubscriptionDto,
    SquadInfoDto,
    SubscriptionDto,
    UserDto,
)

T = TypeVar("T", SubscriptionDto, RemnaSubscriptionDto)


class Remnawave(Protocol):
    async def try_connection(self) -> Version: ...

    async def create_user(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
    ) -> UserResponseDto: ...

    async def update_user(
        self,
        user: UserDto,
        uuid: UUID,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
        reset_traffic: bool = False,
    ) -> UserResponseDto: ...

    async def enable_user(self, uuid: UUID) -> None: ...

    async def disable_user(self, uuid: UUID) -> None: ...

    async def delete_user(self, uuid: UUID) -> bool: ...

    async def get_user_by_uuid(self, uuid: UUID) -> Optional[UserResponseDto]: ...

    async def get_users_by_telegram_id(self, telegram_id: int) -> List[UserResponseDto]: ...

    async def get_users_by_email(self, email: str) -> List[UserResponseDto]: ...

    async def get_all_users(self, limit: int, offset: int) -> List[UserResponseDto]: ...

    async def get_devices(self, uuid: UUID) -> List[HwidDeviceDto]: ...

    async def delete_device(self, user_uuid: UUID, hwid: str) -> Optional[int]: ...

    async def delete_all_devices(self, user_uuid: UUID) -> None: ...

    async def drop_connections(self, user_uuid: UUID) -> None: ...

    async def reset_traffic(self, uuid: UUID) -> Optional[UserResponseDto]: ...

    async def revoke_subscription(self, uuid: UUID) -> None: ...

    async def get_squads_available(self) -> bool: ...

    async def get_internal_squads(self) -> List[SquadInfoDto]: ...

    async def get_external_squads(self) -> List[SquadInfoDto]: ...

    async def get_external_squad(
        self,
        uuid: UUID,
    ) -> Optional[GetExternalSquadByUuidResponseDto]: ...

    async def get_nodes(self) -> List[NodeResponseDto]: ...

    async def get_node(self, uuid: UUID) -> Optional[NodeResponseDto]: ...

    async def get_hosts(self) -> List[HostResponseDto]: ...

    async def resolve_legacy_identity(
        self,
        stored: UUID,
        subscription_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[int]: ...
    """Find the panel user behind a uuid written by an older RemnaShop.

    Returns the panel's numeric user id, or None when the user cannot be matched
    at all -- in which case the stored value must be left alone rather than
    replaced with a guess.
    """

    def apply_sync(
        self,
        target: T,
        source: Union[SubscriptionDto, RemnaSubscriptionDto],
    ) -> T: ...
