"""The customer, the shop's public settings, and the cabinet's parts the bot
has no counterpart for (notifications, documents, appearance), answered empty
so that the cabinet hides them."""

from typing import Any, Optional

from aiogram import Bot
from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter
from pydantic import BaseModel

from src.application.common import BotService
from src.application.common.dao import SettingsDao, UserDao
from src.application.common.uow import UnitOfWork
from src.core.config import AppConfig
from src.core.enums import Locale
from src.infrastructure.services.s.bonus import BonusDays

from .auth import bot_username
from .deps import CurrentActor
from .errors import CabinetError

router = APIRouter()


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


@router.get("/users/me")
@inject
async def me(actor: CurrentActor, bonus: FromDishka[BonusDays]) -> dict[str, Any]:
    user = actor.user
    bonus_days = await bonus.kept(user.id) if await bonus.enabled() else 0

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "language": user.language.value,
        "created_at": _iso(getattr(user, "created_at", None)),
        "points": f"{user.points}.00",
        "bonus_days": bonus_days,
        "personal_discount": user.personal_discount,
        "purchase_discount": user.purchase_discount,
        "loyalty_discount": 0,
    }


class LanguageBody(BaseModel):
    language: Optional[str] = None


@router.patch("/users/me")
@inject
async def change_language(
    body: LanguageBody,
    actor: CurrentActor,
    config: FromDishka[AppConfig],
    uow: FromDishka[UnitOfWork],
    user_dao: FromDishka[UserDao],
) -> dict[str, Any]:
    try:
        locale = Locale(body.language or "")
    except ValueError:
        raise CabinetError(422, "VALIDATION_ERROR", "Unknown language") from None
    if locale in config.locales and locale != actor.user.language:
        user = actor.user
        user.language = locale
        async with uow:
            await user_dao.update(user)
            await uow.commit()
    return {"language": locale.value}


@router.get("/users/me/identities")
async def identities(actor: CurrentActor) -> dict[str, Any]:
    user = actor.user
    label = (
        f"@{user.username}"
        if user.username
        else (str(user.telegram_id) if user.telegram_id is not None else None)
    )
    return {
        "items": [
            {
                "kind": "TELEGRAM",
                "linked": user.telegram_id is not None,
                "label": label,
                "editable": False,
            }
        ]
    }


@router.get("/shop/public-settings")
@inject
async def public_settings(
    settings_dao: FromDishka[SettingsDao],
    bot: FromDishka[Bot],
    bot_service: FromDishka[BotService],
) -> dict[str, Any]:
    settings = await settings_dao.get()
    extra = settings.extra
    channel = settings.requirements.channel_link.get_secret_value().strip()
    channel_url: Optional[str] = None
    if settings.requirements.channel_required and channel:
        channel_url = (
            channel
            if channel.startswith("https://")
            else (f"https://t.me/{channel.removeprefix('@')}")
        )
    support = bot_service.get_support_url()
    return {
        # the bot keeps one subscription a person
        "max_active_subscriptions": 1,
        "device_single_reset_enabled": extra.device_single_reset.enabled,
        "device_all_reset_enabled": extra.device_all_reset.enabled,
        "device_all_reset_cooldown_hours": extra.device_all_reset.cooldown_hours,
        "link_reset_enabled": extra.link_reset.enabled,
        "bot_username": await bot_username(bot),
        "channel_url": channel_url,
        "support_telegram": None,
        "support_url": support if support.startswith("https://") else None,
        "payments_enabled": settings.access.payments_allowed,
    }


_APPEARANCE = {
    "brand_name": None,
    "primary_color": None,
    "shader_base": None,
    "shader_tones": None,
    "shader_enabled": True,
    "has_logo": False,
    "has_favicon": False,
}


@router.get("/appearance/cabinet")
@router.get("/appearance/dashboard")
async def appearance() -> dict[str, Any]:
    return dict(_APPEARANCE)


@router.get("/legal/documents")
async def legal_documents() -> dict[str, Any]:
    return {"items": [], "total": 0}


@router.get("/cabinet/legal/pending")
async def legal_pending(actor: CurrentActor) -> dict[str, Any]:
    return {"items": [], "total": 0, "is_update": False}


@router.post("/cabinet/legal/actions/accept")
async def legal_accept(actor: CurrentActor) -> dict[str, Any]:
    return {"items": [], "total": 0, "is_update": False}


@router.get("/notifications/me")
async def notifications(actor: CurrentActor) -> dict[str, Any]:
    return {"items": [], "unread_count": 0}


@router.post("/notifications/me/actions/mark-read", status_code=204)
@router.post("/notifications/me/actions/clear", status_code=204)
async def notifications_noop(actor: CurrentActor) -> None:
    return None


@router.get("/notifications/me/push/public-key")
async def push_key(actor: CurrentActor) -> dict[str, Any]:
    return {"public_key": None}


@router.get("/notifications/me/push-subscriptions")
async def push_subscriptions(actor: CurrentActor) -> dict[str, Any]:
    return {"items": []}


@router.get("/shop/connect-guide")
async def connect_guide() -> dict[str, Any]:
    return {"platforms": []}


@router.post("/shop/connect-guide/verify")
async def connect_verify() -> dict[str, Any]:
    return {"valid": False}
