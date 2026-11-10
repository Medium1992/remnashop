from typing import Final

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.state import StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode, StartMode
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier
from src.application.common.dao import SubscriptionDao
from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import TelegramUserDto
from src.application.use_cases.promocode.queries.validate import (
    ValidatePromocode,
    ValidatePromocodeDto,
)
from src.application.use_cases.user.queries.plans import GetAvailablePlanByCode
from src.core.constants import GOTO_PREFIX, PAYMENT_PREFIX, TARGET_USER_ID
from src.core.enums import Deeplink, PromocodeRewardType
from src.core.exceptions import (
    PromocodeAlreadyActivatedError,
    PromocodeExpiredError,
    PromocodeNotAvailableError,
    PromocodeNotFoundError,
)
from src.telegram.states import DashboardUser, MainMenu, Subscription, state_from_string

router = Router(name=__name__)

# Callback data comes from the client and can be forged, so only these are
# open to everyone; any other state (the dashboard) needs the dashboard permission.
PUBLIC_STATE_GROUPS: Final[tuple[type[StatesGroup], ...]] = (MainMenu, Subscription)


@router.callback_query(F.data.startswith(GOTO_PREFIX))
async def on_goto(
    callback: CallbackQuery, dialog_manager: DialogManager, user: TelegramUserDto
) -> None:
    logger.info(f"{user.log} Try go to '{callback.data}'")
    data = callback.data.removeprefix(GOTO_PREFIX)  # type: ignore[union-attr]

    if data.startswith(PAYMENT_PREFIX):
        # TODO: Implement a transition to a specific type of payment
        # There shit with data...
        await dialog_manager.bg(
            user_id=user.telegram_id,
            chat_id=user.telegram_id,
        ).start(
            state=Subscription.MAIN,
            mode=StartMode.RESET_STACK,
            show_mode=ShowMode.DELETE_AND_SEND,
        )
        await callback.answer()
        return

    state = state_from_string(data)

    if not state:
        logger.warning(f"{user.log} Trying go to not exist state '{data}'")
        await callback.answer()
        return

    if state.group not in PUBLIC_STATE_GROUPS and not PermissionPolicy.has_permission(
        user, Permission.VIEW_DASHBOARD
    ):
        logger.warning(f"{user.log} Tried to go to '{state}' without the permission")
        await callback.answer()
        return

    if state == DashboardUser.MAIN:
        parts = data.split(":")

        try:
            target_user_id = int(parts[2])
        except ValueError:
            logger.warning(f"{user.log} Invalid target_user_id in callback: {parts[2]}")
            await callback.answer()
            return

        await dialog_manager.bg(
            user_id=user.telegram_id,
            chat_id=user.telegram_id,
        ).start(
            state=DashboardUser.MAIN,
            data={TARGET_USER_ID: target_user_id},
            mode=StartMode.RESET_STACK,
            show_mode=ShowMode.DELETE_AND_SEND,
        )
        logger.debug(f"{user.log} Redirected to user '{target_user_id}'")
        await callback.answer()
        return

    logger.debug(f"{user.log} Redirected to '{state}'")
    await dialog_manager.bg(
        user_id=user.telegram_id,
        chat_id=user.telegram_id,
    ).start(
        state=state,
        mode=StartMode.RESET_STACK,
        show_mode=ShowMode.DELETE_AND_SEND,
    )
    await callback.answer()


@inject
@router.message(CommandStart(deep_link=True, ignore_case=True), F.text.contains(Deeplink.PLAN))
async def on_goto_plan(
    message: Message,
    command: CommandObject,
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    get_available_plan_by_code: FromDishka[GetAvailablePlanByCode],
    notifier: FromDishka[Notifier],
) -> None:
    args = command.args or ""
    public_code = args.removeprefix(Deeplink.PLAN.with_underscore)
    plan = await get_available_plan_by_code(user, public_code)

    # TODO: Handle brootforce of plan codes

    if not plan:
        logger.warning(f"{user.log} Plan with code '{public_code}' not found or not available")
        await notifier.notify_user(user=user, i18n_key="ntf-common.plan-not-found")
        return

    logger.info(f"{user.log} Redirected to plan '{public_code}'")

    await dialog_manager.bg(
        user_id=user.telegram_id,
        chat_id=user.telegram_id,
    ).start(
        state=Subscription.PLAN,
        data={"plan_id": plan.id},
        mode=StartMode.RESET_STACK,
        show_mode=ShowMode.DELETE_AND_SEND,
    )


@router.message(CommandStart(deep_link=True, ignore_case=True), F.text.contains(Deeplink.INVITE))
async def on_goto_invite(
    message: Message,
    command: CommandObject,
    user: TelegramUserDto,
    dialog_manager: DialogManager,
) -> None:
    if command.args == Deeplink.INVITE:
        logger.info(f"{user.log} Redirected to invite menu")
        await dialog_manager.start(
            state=MainMenu.INVITE,
            mode=StartMode.RESET_STACK,
            show_mode=ShowMode.DELETE_AND_SEND,
        )


@inject
@router.message(
    CommandStart(deep_link=True, ignore_case=True),
    F.text.contains(Deeplink.PROMOCODE),
)
async def on_goto_promocode(
    message: Message,
    command: CommandObject,
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    validate_promocode: FromDishka[ValidatePromocode],
    subscription_dao: FromDishka[SubscriptionDao],
    notifier: FromDishka[Notifier],
) -> None:
    args = command.args or ""
    prefix = Deeplink.PROMOCODE.with_underscore
    code = args.removeprefix(prefix).strip().upper() if args.startswith(prefix) else ""

    if not code:
        # Bare `promo` deeplink: open the manual-input dialog.
        logger.info(f"{user.log} Deeplink promocode without code, opening input")
        await dialog_manager.bg(
            user_id=user.telegram_id,
            chat_id=user.telegram_id,
        ).start(
            state=Subscription.PROMOCODE,
            mode=StartMode.RESET_STACK,
            show_mode=ShowMode.DELETE_AND_SEND,
        )
        return

    try:
        promo = await validate_promocode(user, ValidatePromocodeDto(code=code, user=user))
    except PromocodeAlreadyActivatedError:
        await notifier.notify_user(user=user, i18n_key="ntf-promocode.already-activated")
        return
    except PromocodeExpiredError:
        await notifier.notify_user(user=user, i18n_key="ntf-promocode.expired")
        return
    except (PromocodeNotFoundError, PromocodeNotAvailableError):
        await notifier.notify_user(user=user, i18n_key="ntf-promocode.not-found")
        return

    will_replace = False
    if promo.reward_type == PromocodeRewardType.SUBSCRIPTION:
        current = await subscription_dao.get_current(user.id)
        will_replace = current is not None

    logger.info(f"{user.log} Deeplink promocode '{code}' validated, redirecting")

    await dialog_manager.bg(
        user_id=user.telegram_id,
        chat_id=user.telegram_id,
    ).start(
        state=Subscription.PROMOCODE,
        data={
            "prefill_dto": {
                "code": promo.code,
                "reward_type": promo.reward_type.value,
                "reward": promo.reward,
            },
            "prefill_replace": will_replace,
        },
        mode=StartMode.RESET_STACK,
        show_mode=ShowMode.DELETE_AND_SEND,
    )
