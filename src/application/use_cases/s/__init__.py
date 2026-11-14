from typing import Final

from src.application.common import Interactor

from .settings import (
    SetBonusPlan,
    SetRulesText,
    SetSSetting,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
)

S_USE_CASES: Final[tuple[type[Interactor], ...]] = (
    SetBonusPlan,
    SetRulesText,
    SetSSetting,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
)
