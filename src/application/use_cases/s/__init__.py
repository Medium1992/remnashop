from typing import Final

from src.application.common import Interactor

from .settings import SetSSetting, ToggleSSetting, UpdateRouterHwids, UpdateVersionChannel

S_USE_CASES: Final[tuple[type[Interactor], ...]] = (
    SetSSetting,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
)
