from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.s_kept import KEPT_FIELDS, KeptLimitsDto
from src.infrastructure.database.models.s import SKeptLimits


class KeptLimitsDao:
    """Which hand-set values of a subscription stay on renewal (``s_kept_limits``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, subscription_id: int) -> KeptLimitsDto:
        # In a savepoint: read in the middle of a renewal, a failure here must
        # not leave the renewal's transaction aborted.
        async with self.session.begin_nested():
            row = await self.session.scalar(
                select(SKeptLimits).where(SKeptLimits.subscription_id == subscription_id)
            )
        if row is None:
            return KeptLimitsDto()
        return KeptLimitsDto(devices=row.devices, traffic=row.traffic, squads=row.squads)

    async def set(self, subscription_id: int, name: str, value: bool) -> None:
        if name not in KEPT_FIELDS:
            raise ValueError(f"Unknown kept field '{name}'")
        stmt = insert(SKeptLimits).values(subscription_id=subscription_id, **{name: value})
        stmt = stmt.on_conflict_do_update(
            index_elements=[SKeptLimits.subscription_id], set_={name: value}
        )
        await self.session.execute(stmt)
