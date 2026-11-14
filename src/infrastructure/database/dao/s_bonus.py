from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import ReferralRewardType
from src.infrastructure.database.models import ReferralReward


class BonusDaysDao:
    """Referral days not yet taken: rewards of days still marked not issued."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _pending(self, user_id: int) -> list:
        return [
            ReferralReward.user_id == user_id,
            ReferralReward.type == ReferralRewardType.EXTRA_DAYS,
            ReferralReward.is_issued.is_(False),
            ReferralReward.amount > 0,
        ]

    async def pending(self, user_id: int) -> int:
        stmt = select(func.coalesce(func.sum(ReferralReward.amount), 0)).where(
            *self._pending(user_id)
        )
        return int(await self.session.scalar(stmt) or 0)

    async def claim(self, user_id: int) -> dict[int, int]:
        """Mark what is pending as issued; the ids taken and their days.

        One statement, so two taps at once never take the same reward twice.
        """
        stmt = (
            update(ReferralReward)
            .where(*self._pending(user_id))
            .values(is_issued=True)
            .returning(ReferralReward.id, ReferralReward.amount)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row.id: row.amount for row in rows}

    async def release(self, reward_ids: list[int]) -> None:
        """Give back what was claimed when it could not be granted."""
        if reward_ids:
            stmt = (
                update(ReferralReward)
                .where(ReferralReward.id.in_(reward_ids))
                .values(is_issued=False)
            )
            await self.session.execute(stmt)
