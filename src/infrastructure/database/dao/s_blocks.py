from datetime import datetime
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.s_blocks import UserBlockDto
from src.infrastructure.database.models.s import SUserBlock


def _to_dto(row: SUserBlock) -> UserBlockDto:
    return UserBlockDto(
        user_id=row.user_id,
        telegram_id=row.telegram_id,
        reason=row.reason,
        until=row.until,
        created_by=row.created_by,
        created_at=row.created_at,
    )


class UserBlockDao:
    """Temporary blocks, one row per blocked user (``s_user_blocks``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: int) -> Optional[UserBlockDto]:
        row = await self.session.scalar(select(SUserBlock).where(SUserBlock.user_id == user_id))
        return _to_dto(row) if row else None

    async def all(self) -> list[UserBlockDto]:
        rows = await self.session.scalars(select(SUserBlock).order_by(SUserBlock.created_at))
        return [_to_dto(row) for row in rows]

    async def user_ids(self) -> set[int]:
        return set(await self.session.scalars(select(SUserBlock.user_id)))

    async def due(self, now: datetime) -> list[UserBlockDto]:
        stmt = select(SUserBlock).where(SUserBlock.until.is_not(None), SUserBlock.until <= now)
        return [_to_dto(row) for row in await self.session.scalars(stmt)]

    async def put(self, block: UserBlockDto) -> None:
        """Create the block, or replace the one the user already has."""
        values = {
            "user_id": block.user_id,
            "telegram_id": block.telegram_id,
            "reason": block.reason,
            "until": block.until,
            "created_by": block.created_by,
        }
        stmt = insert(SUserBlock).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SUserBlock.user_id],
            set_={k: v for k, v in values.items() if k != "user_id"},
        )
        await self.session.execute(stmt)

    async def remove(self, user_id: int) -> bool:
        result = await self.session.execute(delete(SUserBlock).where(SUserBlock.user_id == user_id))
        return bool(getattr(result, "rowcount", 0))
