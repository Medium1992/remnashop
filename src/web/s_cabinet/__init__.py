"""The web cabinet's API (docs/s/README.md, src/core/utils/s_cabinet.py).

It speaks the API of the upstream cabinet (github.com/remnashop/cabinet) on top
of this bot: the same paths and answers, the bot's own logic behind them.
Mounted only when the cabinet is configured, and answering only requests that
come through the cabinet's nginx (``X-Cabinet-Proxy-Secret``).
"""

from fastapi import APIRouter, Depends, FastAPI
from loguru import logger

from src.core.constants import API_V1
from src.core.utils.s_cabinet import cabinet_config

from . import auth, checkout, profile, referrals, subscriptions
from .deps import require_cabinet_proxy
from .errors import CabinetError, handle_cabinet_error


def mount_cabinet(app: FastAPI) -> None:
    config = cabinet_config()
    if config is None:
        return
    router = APIRouter(prefix=API_V1, dependencies=[Depends(require_cabinet_proxy)])
    for module in (auth, profile, subscriptions, checkout, referrals):
        router.include_router(module.router)
    app.include_router(router, include_in_schema=False)
    app.add_exception_handler(CabinetError, handle_cabinet_error)
    logger.info(f"Web cabinet API on for '{config.domain}'")
