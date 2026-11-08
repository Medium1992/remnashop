"""Temporary slot grants and the limit arithmetic of strict slots.

Kept apart from the service so the panel client can use them without an
import cycle.
"""

import json
import time
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from loguru import logger
from redis.asyncio import Redis

from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import SlotGrantKey

# A grant whose user cannot be read this many sweeps in a row is dropped, so a
# user deleted in the panel does not pin a +1 forever.
MAX_LOOKUP_MISSES = 5
PANEL_CONCURRENCY = 5


def physical_limit(
    ceiling: int,
    occupied: int,
    base_limit: int,
    granted: Optional[int] = None,
) -> int:
    """The limit to keep in the panel.

    ``ceiling`` 0 is an unlimited plan, which strict slots leave alone. The
    limit never drops below what is occupied: lowering a plan does not
    disconnect devices, it only stops new ones.
    """
    if ceiling <= 0:
        return 0
    limit = max(min(base_limit, ceiling), occupied)
    if granted is not None:
        limit = max(limit, granted)
    return limit


def shown_counters(ceiling: int, occupied: int) -> tuple[int, int]:
    """(occupied, shown limit) for the user: the ceiling, never below occupied.

    A lowered ceiling reads "3 of 3" rather than a nonsensical "3 of 2".
    """
    return occupied, max(ceiling, occupied)


@dataclass(kw_only=True)
class SlotGrant:
    telegram_id: Optional[int]
    original_limit: int
    granted_limit: int
    device_count_before: int
    expires_at: float
    misses: int = 0

    @property
    def is_open(self) -> bool:
        return self.expires_at > time.time()

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))


def grant_key(remna_id: UUID) -> str:
    return serialize_storage_key(SlotGrantKey(remna_id=str(remna_id)))


async def read_grant(redis: Redis, remna_id: UUID) -> Optional[SlotGrant]:
    raw = await redis.get(grant_key(remna_id))
    if not raw:
        return None
    try:
        return SlotGrant(**json.loads(raw))
    except (TypeError, ValueError):
        logger.warning(f"Unreadable slot grant for '{remna_id}' dropped")
        await redis.delete(grant_key(remna_id))
        return None
