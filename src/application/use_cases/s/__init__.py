from typing import Final

from src.application.common import Interactor

from .payments import ConfirmPaymentByHand
from .settings import (
    SetBonusPlan,
    SetRulesText,
    SetSSetting,
    ToggleReceipts,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
)

S_USE_CASES: Final[tuple[type[Interactor], ...]] = (
    ConfirmPaymentByHand,
    SetBonusPlan,
    SetRulesText,
    SetSSetting,
    ToggleReceipts,
    ToggleSSetting,
    UpdateRouterHwids,
    UpdateVersionChannel,
)
