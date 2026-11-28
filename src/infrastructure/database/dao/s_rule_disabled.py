from typing import Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.models.s import SRuleDisabled


class RuleDisabledDao:
    """Panel users switched off by the inactive-profiles rule (``s_rule_disabled``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, user_remna_id: UUID, user_id: int) -> None:
        stmt = insert(SRuleDisabled).values(user_remna_id=user_remna_id, user_id=user_id)
        await self.session.execute(stmt.on_conflict_do_nothing())

    async def find(self, user_remna_id: UUID) -> Optional[int]:
        """The user the panel user was switched off for, if it was this rule."""
        user_id = await self.session.scalar(
            select(SRuleDisabled.user_id).where(SRuleDisabled.user_remna_id == user_remna_id)
        )
        return int(user_id) if user_id is not None else None

    async def remove(self, user_remna_id: UUID) -> None:
        await self.session.execute(
            delete(SRuleDisabled).where(SRuleDisabled.user_remna_id == user_remna_id)
        )
