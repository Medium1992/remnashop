from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.core.utils.time import datetime_now


@dataclass(kw_only=True)
class UserBlockDto:
    user_id: int
    telegram_id: Optional[int] = None
    reason: str = ""
    # None: until lifted by hand.
    until: Optional[datetime] = None
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None

    @property
    def is_permanent(self) -> bool:
        return self.until is None

    @property
    def is_over(self) -> bool:
        return self.until is not None and self.until <= datetime_now()
