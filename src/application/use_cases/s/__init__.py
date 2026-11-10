from typing import Final

from src.application.common import Interactor

from .settings import (
    SetRulesText,
    SetSSetting,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
)

S_USE_CASES: Final[tuple[type[Interactor], ...]] = (
    SetRulesText,
    SetSSetting,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
)
