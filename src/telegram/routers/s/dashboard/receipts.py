"""Receipts in the S dashboard: the switch, the login to "Мой налог", and the
receipts that need an admin (docs/s/README.md)."""

import html
from datetime import datetime
from typing import Any, Final, Optional

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.text import Format
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger
from magic_filter import F

from src.application.common import Notifier, TranslatorRunner
from src.application.common.dao import UserDao
from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.application.dto.s_receipts import NalogLogin, ReceiptDto, ReceiptStatus
from src.application.use_cases.s.settings import ToggleReceipts
from src.core.constants import USER_KEY
from src.core.enums import BannerName
from src.infrastructure.services.s.nalog import MOSCOW, NalogError, receipt_url
from src.infrastructure.services.s.nalog_auth import (
    NalogAuth,
    is_inn,
    is_payer_inn,
    normalize_proxy,
)
from src.infrastructure.services.s.receipt_import import ImportPlan
from src.infrastructure.services.s.receipts import Receipts
from src.telegram.s_states import SDashboard
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Button, Row, ScrollingGroup, Select, SwitchTo, Url
from src.telegram.window import Window

# What the input window is waiting for.
INPUT: Final[str] = "s_receipts_input"
LOGIN_INN: Final[str] = "s_receipts_inn"
LOGIN_PHONE: Final[str] = "s_receipts_phone"
CHALLENGE: Final[str] = "s_receipts_challenge"
LOGIN_DEVICE: Final[str] = "s_receipts_device"
IMPORT_SINCE: Final[str] = "s_receipts_import_since"
IMPORT_KEYS: Final[str] = "s_receipts_import_keys"
IMPORT_VIEW: Final[str] = "s_receipts_import_view"
IMPORT_SHOWN: Final[int] = 20
LIST_KIND: Final[str] = "s_receipts_list"  # "unissued", "undelivered" or a user id
ITEM: Final[str] = "s_receipts_item"

PROXY_SCHEMES: Final[tuple[str, ...]] = ("socks5://", "socks4://", "http://", "https://")


def moscow(moment: datetime) -> str:
    return f"{moment.astimezone(MOSCOW):%d.%m.%Y %H:%M}"


def _actor(dialog_manager: DialogManager) -> TelegramUserDto:
    return dialog_manager.middleware_data[USER_KEY]  # type: ignore[no-any-return]


def _allowed(dialog_manager: DialogManager) -> bool:
    # The S dashboard is only shown to those with it; checked again on each
    # action all the same, since these touch the tax cabinet.
    user = _actor(dialog_manager)
    if PermissionPolicy.has_permission(user, Permission.SETTINGS_EXTRA):
        return True
    logger.warning(f"{user.log} Receipts action refused: no permission")
    return False


async def _tell(notifier: Notifier, user: TelegramUserDto, key: str, **kw: Any) -> None:
    await notifier.notify_user(user, payload=MessagePayloadDto(i18n_key=key, i18n_kwargs=kw))


# -- the main window -----------------------------------------------------------


@inject
async def receipts_getter(
    dialog_manager: DialogManager,
    receipts: FromDishka[Receipts],
    auth: FromDishka[NalogAuth],
    **kwargs: Any,
) -> dict[str, Any]:
    account = await auth.account()
    summary = await receipts.summary()
    return {
        "receipts_enabled": int(await receipts.enabled()),
        "logged_in": int(account.logged_in),
        "lost": int(account.lost_at is not None),
        "lost_reason": html.escape(account.lost_reason or ""),
        "method": (account.method or NalogLogin.PASSWORD).value,
        "inn": account.inn or "",
        "proxy_set": int(bool(account.proxy)),
        "issued": summary.issued,
        "waiting": summary.waiting,
        "unissued": summary.unissued,
        "unissued_amount": str(summary.unissued_amount),
        "undelivered": summary.undelivered,
        "undelivered_amount": str(summary.undelivered_amount),
    }


@inject
async def on_receipts_toggle(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    toggle: FromDishka[ToggleReceipts],
) -> None:
    await toggle(_actor(dialog_manager), None)


async def on_ask(callback: CallbackQuery, widget: Any, dialog_manager: DialogManager) -> None:
    """A button that asks for text: its id says what for."""
    kind = widget.widget_id.removeprefix("r_ask_")
    dialog_manager.dialog_data[INPUT] = (
        "inn" if kind in ("password", "sms", "browser", "key") else kind
    )
    dialog_manager.dialog_data["s_receipts_login"] = kind
    await dialog_manager.switch_to(SDashboard.RECEIPTS_INPUT)


@inject
async def on_logout(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    auth: FromDishka[NalogAuth],
    notifier: FromDishka[Notifier],
) -> None:
    if not _allowed(dialog_manager):
        return
    await auth.logout()
    await _tell(notifier, _actor(dialog_manager), "ntf-s-receipts.logged-out")


async def on_list(callback: CallbackQuery, widget: Any, dialog_manager: DialogManager) -> None:
    dialog_manager.dialog_data[LIST_KIND] = widget.widget_id.removeprefix("r_list_")
    await dialog_manager.switch_to(SDashboard.RECEIPTS_LIST)


# -- the input window ----------------------------------------------------------


async def input_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    kind = dialog_manager.dialog_data.get(INPUT, "inn")
    return {"input": kind, "is_code": int(kind == "code")}


@inject
async def on_new_code(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    auth: FromDishka[NalogAuth],
    notifier: FromDishka[Notifier],
) -> None:
    """A fresh SMS for the same phone: the old code expired or was mistyped."""
    if not _allowed(dialog_manager):
        return
    data = dialog_manager.dialog_data
    user = _actor(dialog_manager)
    try:
        challenge = await auth.sms_start(data.get(LOGIN_PHONE, ""))
    except NalogError as error:
        logger.warning(f"{user.log} Tax cabinet refused a new SMS: {error}")
        await _tell(notifier, user, "ntf-s-receipts.cabinet", reason=html.escape(str(error)))
        return
    data[CHALLENGE] = challenge.token
    await _tell(notifier, user, "ntf-s-receipts.code-sent")


async def _drop(message: Message) -> None:
    # A password or a code stays in the chat no longer than it takes to read it.
    try:
        await message.delete()
    except Exception as error:
        logger.warning(f"Message with a secret not deleted: {error}")


@inject
async def on_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    auth: FromDishka[NalogAuth],
    receipts: FromDishka[Receipts],
    user_dao: FromDishka[UserDao],
    notifier: FromDishka[Notifier],
) -> None:
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND
    if not _allowed(dialog_manager):
        return
    data = dialog_manager.dialog_data
    kind = data.get(INPUT, "")
    text = (message.text or "").strip()
    user = _actor(dialog_manager)
    if kind in ("password", "code", "proxy", "refresh", "key"):
        await _drop(message)

    try:
        notice = await _handle_input(kind, text, data, dialog_manager, auth, receipts, user_dao)
        if notice is not None:
            key, kw = notice
            data.pop(INPUT, None)
            await dialog_manager.switch_to(SDashboard.RECEIPTS)
            await _tell(notifier, user, key, **kw)
    except _RefusedError as refused:
        await _tell(notifier, user, f"ntf-s-receipts.{refused}")
    except NalogError as error:
        logger.warning(f"{user.log} Tax cabinet refused at '{kind}': {error}")
        key = "ntf-s-receipts.code-refused" if kind == "code" else "ntf-s-receipts.cabinet"
        await _tell(notifier, user, key, reason=html.escape(str(error)))
    except Exception as error:
        logger.exception(f"{user.log} Receipts input '{kind}' failed")
        await _tell(notifier, user, "ntf-s-receipts.cabinet", reason=html.escape(str(error)))


class _RefusedError(Exception):
    """What was typed won't do; the value is the notice's i18n suffix."""


async def _handle_input(  # noqa: C901
    kind: str,
    text: str,
    data: dict[str, Any],
    dialog_manager: DialogManager,
    auth: NalogAuth,
    receipts: Receipts,
    user_dao: UserDao,
) -> Optional[tuple[str, dict[str, Any]]]:
    """Does what was typed for; a notice to show from the main window, or None
    when the dialog goes on elsewhere."""
    if kind == "inn":
        if not is_inn(text):
            raise _RefusedError("inn-invalid")
        data[LOGIN_INN] = text
        data[INPUT] = {"password": "password", "browser": "device", "key": "key"}.get(
            data.get("s_receipts_login", ""), "phone"
        )
        return None

    if kind == "password":
        await auth.login_password(data.get(LOGIN_INN, ""), text)
        return "ntf-s-receipts.logged-in", {}

    if kind == "phone":
        challenge = await auth.sms_start(text)
        data[LOGIN_PHONE] = text
        data[CHALLENGE] = challenge.token
        data[INPUT] = "code"
        return None

    if kind == "code":
        await auth.sms_verify(
            data.get(LOGIN_INN, ""), data.get(LOGIN_PHONE, ""), text, data.get(CHALLENGE, "")
        )
        data.pop(CHALLENGE, None)
        return "ntf-s-receipts.logged-in", {}

    if kind == "key":
        key = text.strip().strip('"').removeprefix("Bearer ").strip()
        if len(key) < 16 or " " in key:
            raise _RefusedError("key-invalid")
        await auth.login_key(data.get(LOGIN_INN, ""), key)
        return "ntf-s-receipts.logged-in", {}

    if kind == "device":
        device_id = text.strip().strip('"')
        if not device_id or len(device_id) > 64 or " " in device_id:
            raise _RefusedError("device-invalid")
        data[LOGIN_DEVICE] = device_id
        data[INPUT] = "refresh"
        return None

    if kind == "refresh":
        refresh_token = text.strip().strip('"')
        if len(refresh_token) < 16 or " " in refresh_token:
            raise _RefusedError("refresh-invalid")
        await auth.login_browser(data.get(LOGIN_INN, ""), data.get(LOGIN_DEVICE, ""), refresh_token)
        data.pop(LOGIN_DEVICE, None)
        return "ntf-s-receipts.logged-in", {}

    if kind == "proxy":
        if text != "-" and not normalize_proxy(text).startswith(PROXY_SCHEMES):
            raise _RefusedError("proxy-invalid")
        await auth.set_proxy(None if text == "-" else text)
        return "ntf-s-receipts.saved", {}

    if kind == "since":
        try:
            day = datetime.strptime(text, "%d.%m.%Y").replace(tzinfo=MOSCOW)
        except ValueError as e:
            raise _RefusedError("date-invalid") from e
        return "ntf-s-receipts.added", {"count": await receipts.add_since(day)}

    if kind == "import":
        try:
            day = datetime.strptime(text, "%d.%m.%Y").replace(tzinfo=MOSCOW)
        except ValueError as e:
            raise _RefusedError("date-invalid") from e
        plan = await receipts.import_plan(day)
        data[IMPORT_SINCE] = day.isoformat()
        data[IMPORT_KEYS] = [pair.key for pair in plan.pairs]
        data[IMPORT_VIEW] = _import_view(plan)
        data.pop(INPUT, None)
        await dialog_manager.switch_to(SDashboard.RECEIPTS_IMPORT)
        return None

    if kind == "find":
        found = None
        if text.lstrip("-").isdigit():
            found = await user_dao.get_by_telegram_id(int(text))
        if found is None:
            raise _RefusedError("user-not-found")
        data[LIST_KIND] = str(found.id)
        data.pop(INPUT, None)
        await dialog_manager.switch_to(SDashboard.RECEIPTS_LIST)
        return None

    if kind == "manual":
        try:
            saved = await receipts.set_manual(int(data.get(ITEM, 0)), text)
        except ValueError as e:
            raise _RefusedError("number-invalid") from e
        if saved is None:
            raise _RefusedError("changed")
        data.pop(INPUT, None)
        await dialog_manager.switch_to(SDashboard.RECEIPTS_ITEM)
        return None

    if kind == "org":
        inn, _, name = text.partition(" ")
        if not is_payer_inn(inn) or not name.strip():
            raise _RefusedError("org-invalid")
        await receipts.reissue_to_organisation(int(data.get(ITEM, 0)), inn, name.strip())
        data.pop(INPUT, None)
        await dialog_manager.switch_to(SDashboard.RECEIPTS_ITEM)
        return None

    raise _RefusedError("changed")


# -- receipts made by hand ------------------------------------------------------


def _import_view(plan: ImportPlan) -> dict[str, Any]:
    """What the preview shows, kept in the dialog until confirmed."""
    lines = [
        f"{pair.income.amount} ₽: оплата {moscow(pair.payment['paid_at'])} ↔ "
        f"чек {pair.income.receipt_uuid} {moscow(pair.income.operation_time)}"
        for pair in plan.pairs[:IMPORT_SHOWN]
    ]
    return {
        "paired": len(plan.pairs),
        "paired_amount": str(plan.paired_amount),
        "lone_payments": len(plan.lone_payments),
        "lone_payments_amount": str(plan.lone_payments_amount),
        "lone_incomes": len(plan.lone_incomes),
        "lone_incomes_amount": str(plan.lone_incomes_amount),
        "lines": "\n".join(lines),
        "more": max(len(plan.pairs) - IMPORT_SHOWN, 0),
    }


async def import_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    view = dict(dialog_manager.dialog_data.get(IMPORT_VIEW) or {})
    view.setdefault("paired", 0)
    view["lines"] = html.escape(str(view.get("lines", "")))
    return view


@inject
async def on_import_confirm(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    receipts: FromDishka[Receipts],
    notifier: FromDishka[Notifier],
) -> None:
    if not _allowed(dialog_manager):
        return
    data = dialog_manager.dialog_data
    user = _actor(dialog_manager)
    since = data.get(IMPORT_SINCE)
    keys = set(data.get(IMPORT_KEYS) or [])
    if not since or not keys:
        return
    try:
        added = await receipts.apply_import(datetime.fromisoformat(since), keys)
    except Exception as error:
        logger.exception(f"{user.log} Receipts made by hand not tied")
        await _tell(notifier, user, "ntf-s-receipts.cabinet", reason=html.escape(str(error)))
        return
    for key in (IMPORT_SINCE, IMPORT_KEYS, IMPORT_VIEW):
        data.pop(key, None)
    await dialog_manager.switch_to(SDashboard.RECEIPTS)
    await _tell(notifier, user, "ntf-s-receipts.imported", count=added)


# -- the list and one receipt --------------------------------------------------


def _item(receipt: ReceiptDto) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "amount": str(receipt.amount),
        "date": moscow(receipt.paid_at),
        "status": receipt.status.value,
        "undelivered": int(bool(receipt.delivery_error)),
    }


@inject
async def list_getter(
    dialog_manager: DialogManager,
    receipts: FromDishka[Receipts],
    **kwargs: Any,
) -> dict[str, Any]:
    kind = dialog_manager.dialog_data.get(LIST_KIND, "unissued")
    if kind in ("unissued", "undelivered"):
        items = await receipts.unissued(undelivered=kind == "undelivered")
    else:
        items = await receipts.of_user(int(kind), issued_only=False)
    return {
        "kind": kind if kind in ("unissued", "undelivered") else "user",
        "receipts": [_item(receipt) for receipt in items],
        "count": len(items),
    }


async def on_item(
    callback: CallbackQuery, widget: Any, dialog_manager: DialogManager, item_id: str
) -> None:
    dialog_manager.dialog_data[ITEM] = int(item_id)
    await dialog_manager.switch_to(SDashboard.RECEIPTS_ITEM)


async def _current(dialog_manager: DialogManager, receipts: Receipts) -> Optional[ReceiptDto]:
    return await receipts.get(int(dialog_manager.dialog_data.get(ITEM, 0)))


@inject
async def item_getter(
    dialog_manager: DialogManager,
    receipts: FromDishka[Receipts],
    user_dao: FromDishka[UserDao],
    i18n: FromDishka[TranslatorRunner],
    **kwargs: Any,
) -> dict[str, Any]:
    receipt = await _current(dialog_manager, receipts)
    if receipt is None:
        return {"found": 0, "status": "", "has_url": 0}
    buyer = await user_dao.get_by_id(receipt.user_id) if receipt.user_id else None
    issued = receipt.status in (ReceiptStatus.REGISTERED, ReceiptStatus.DELIVERED)
    lines = [
        (
            "client",
            f"{receipt.client_name} (ИНН {receipt.client_inn})" if receipt.client_inn else "",
        ),
        ("number", receipt.receipt_uuid or ""),
        ("error", receipt.error or ""),
        ("delivery", receipt.delivery_error or ""),
    ]
    details = "".join(
        "\n" + i18n.get(f"msg-s-receipts.item-{key}", value=html.escape(value))
        for key, value in lines
        if value
    )
    if receipt.status == ReceiptStatus.UNKNOWN:
        details += "\n\n" + i18n.get("msg-s-receipts.item-check")
    return {
        "found": 1,
        "amount": str(receipt.amount),
        "date": moscow(receipt.paid_at),
        "status": receipt.status.value,
        "name": html.escape(receipt.name or "—"),
        "buyer": html.escape(buyer.name if buyer else "—"),
        "telegram_id": (buyer.telegram_id if buyer else None) or 0,
        "details": details,
        "has_url": int(bool(receipt.receipt_uuid and receipt.inn and issued)),
        "url": receipt_url(receipt.inn, receipt.receipt_uuid)
        if receipt.receipt_uuid and receipt.inn
        else "",
        "can_retry": int(receipt.status in (ReceiptStatus.FAILED, ReceiptStatus.UNKNOWN)),
        "can_resend": int(issued),
        "can_reissue": int(issued and not receipt.client_inn),
    }


@inject
async def on_retry(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    receipts: FromDishka[Receipts],
    notifier: FromDishka[Notifier],
) -> None:
    if not _allowed(dialog_manager):
        return
    done = await receipts.retry(int(dialog_manager.dialog_data.get(ITEM, 0)))
    key = "ntf-s-receipts.queued" if done else "ntf-s-receipts.changed"
    await _tell(notifier, _actor(dialog_manager), key)


@inject
async def on_resend(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    receipts: FromDishka[Receipts],
    notifier: FromDishka[Notifier],
) -> None:
    if not _allowed(dialog_manager):
        return
    sent = await receipts.redeliver(int(dialog_manager.dialog_data.get(ITEM, 0)))
    key = "ntf-s-receipts.resent" if sent else "ntf-s-receipts.not-sent"
    await _tell(notifier, _actor(dialog_manager), key)


async def on_item_ask(callback: CallbackQuery, widget: Any, dialog_manager: DialogManager) -> None:
    dialog_manager.dialog_data[INPUT] = widget.widget_id.removeprefix("r_item_")
    await dialog_manager.switch_to(SDashboard.RECEIPTS_INPUT)


async def on_input_back(
    callback: CallbackQuery, widget: Any, dialog_manager: DialogManager
) -> None:
    kind = dialog_manager.dialog_data.pop(INPUT, "")
    back = SDashboard.RECEIPTS_ITEM if kind in ("manual", "org") else SDashboard.RECEIPTS
    await dialog_manager.switch_to(back)


# -- windows -------------------------------------------------------------------

receipts_main = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-receipts.main"),
    Row(
        Button(
            I18nFormat("btn-s.receipts-enabled", enabled=F["receipts_enabled"]),
            id="receipts_enabled",
            on_click=on_receipts_toggle,
        )
    ),
    Row(Button(I18nFormat("btn-s-receipts.login-key"), id="r_ask_key", on_click=on_ask)),
    Row(Button(I18nFormat("btn-s-receipts.login-browser"), id="r_ask_browser", on_click=on_ask)),
    Row(
        Button(I18nFormat("btn-s-receipts.login-password"), id="r_ask_password", on_click=on_ask),
        Button(I18nFormat("btn-s-receipts.login-sms"), id="r_ask_sms", on_click=on_ask),
    ),
    Row(
        Button(I18nFormat("btn-s-receipts.proxy"), id="r_ask_proxy", on_click=on_ask),
        Button(
            I18nFormat("btn-s-receipts.logout"),
            id="r_logout",
            on_click=on_logout,
            when=F["logged_in"] | F["lost"],
        ),
    ),
    Row(
        Button(
            I18nFormat("btn-s-receipts.unissued", count=F["unissued"]),
            id="r_list_unissued",
            on_click=on_list,
        ),
        Button(
            I18nFormat("btn-s-receipts.undelivered", count=F["undelivered"]),
            id="r_list_undelivered",
            on_click=on_list,
        ),
    ),
    Row(Button(I18nFormat("btn-s-receipts.import"), id="r_ask_import", on_click=on_ask)),
    Row(
        Button(I18nFormat("btn-s-receipts.find"), id="r_ask_find", on_click=on_ask),
        Button(I18nFormat("btn-s-receipts.since"), id="r_ask_since", on_click=on_ask),
    ),
    Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=SDashboard.MAIN)),
    IgnoreUpdate(),
    state=SDashboard.RECEIPTS,
    getter=receipts_getter,
)

receipts_input = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-receipts.input"),
    Row(
        Button(
            I18nFormat("btn-s-receipts.new-code"),
            id="r_new_code",
            on_click=on_new_code,
            when=F["is_code"],
        )
    ),
    Row(Button(I18nFormat("btn-back.general"), id="back", on_click=on_input_back)),
    MessageInput(func=on_input),
    IgnoreUpdate(),
    state=SDashboard.RECEIPTS_INPUT,
    getter=input_getter,
)

receipts_import = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-receipts.import"),
    Row(
        Button(
            I18nFormat("btn-s-receipts.import-confirm", count=F["paired"]),
            id="r_import_confirm",
            on_click=on_import_confirm,
            when=F["paired"],
        )
    ),
    Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=SDashboard.RECEIPTS)),
    IgnoreUpdate(),
    state=SDashboard.RECEIPTS_IMPORT,
    getter=import_getter,
)

receipts_list = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-receipts.list"),
    ScrollingGroup(
        Select(
            I18nFormat(
                "btn-s-receipts.item",
                amount=F["item"]["amount"],
                date=F["item"]["date"],
                status=F["item"]["status"],
                undelivered=F["item"]["undelivered"],
            ),
            id="r_item",
            item_id_getter=lambda item: item["id"],
            items="receipts",
            on_click=on_item,
        ),
        id="r_items",
        width=1,
        height=8,
        hide_on_single_page=True,
    ),
    Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=SDashboard.RECEIPTS)),
    IgnoreUpdate(),
    state=SDashboard.RECEIPTS_LIST,
    getter=list_getter,
)

receipts_item = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-s-receipts.item"),
    Row(Url(I18nFormat("btn-s-receipts.open"), url=Format("{url}"), when=F["has_url"])),
    Row(
        Button(
            I18nFormat("btn-s-receipts.retry"), id="r_retry", on_click=on_retry, when=F["can_retry"]
        )
    ),
    Row(
        Button(
            I18nFormat("btn-s-receipts.manual"),
            id="r_item_manual",
            on_click=on_item_ask,
            when=F["can_retry"],
        )
    ),
    Row(
        Button(
            I18nFormat("btn-s-receipts.resend"),
            id="r_resend",
            on_click=on_resend,
            when=F["can_resend"],
        )
    ),
    Row(
        Button(
            I18nFormat("btn-s-receipts.reissue"),
            id="r_item_org",
            on_click=on_item_ask,
            when=F["can_reissue"],
        )
    ),
    Row(SwitchTo(I18nFormat("btn-back.general"), id="back", state=SDashboard.RECEIPTS_LIST)),
    IgnoreUpdate(),
    state=SDashboard.RECEIPTS_ITEM,
    getter=item_getter,
)
