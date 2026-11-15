"""The buyer's receipts (docs/s/README.md).

A receipt pressed is fetched from the tax service and sent as a file; it is
held in memory only, nothing is written to the disk.
"""

from typing import Any

from aiogram.types import BufferedInputFile, CallbackQuery
from aiogram_dialog import Dialog, DialogManager, ShowMode
from dishka import AsyncContainer, FromDishka
from dishka.integrations.aiogram import CONTAINER_NAME
from dishka.integrations.aiogram_dialog import inject
from loguru import logger
from magic_filter import F

from src.application.common import Notifier, TranslatorRunner
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.core.constants import USER_KEY
from src.core.enums import BannerName
from src.infrastructure.database.dao.s_receipts import ReceiptDao
from src.infrastructure.services.s.nalog import receipt_url
from src.infrastructure.services.s.receipts import Receipts, file_name
from src.telegram.routers.s.dashboard.receipts import moscow
from src.telegram.s_states import SReceipts
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Cancel, Row, ScrollingGroup, Select
from src.telegram.window import Window


async def _flags(dialog_manager: DialogManager) -> tuple[bool, int]:
    container: AsyncContainer = dialog_manager.middleware_data[CONTAINER_NAME]
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    receipts = await container.get(Receipts)
    try:
        if not await receipts.enabled():
            return False, 0
        return True, await (await container.get(ReceiptDao)).count_of_user(user.id)
    except Exception as error:
        # A receipts failure never takes the subscription window down.
        logger.warning(f"{user.log} Receipts flags not read: {error}")
        return False, 0


async def receipts_user_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    """For the subscription window: the "My receipts" button."""
    enabled, count = await _flags(dialog_manager)
    return {"s_receipts": int(enabled and count > 0)}


async def receipts_note_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    """For the payment window: payments are taken from private persons only."""
    enabled, _ = await _flags(dialog_manager)
    return {"s_receipts_on": int(enabled)}


@inject
async def my_receipts_getter(
    dialog_manager: DialogManager,
    receipts: FromDishka[Receipts],
    **kwargs: Any,
) -> dict[str, Any]:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    items = await receipts.of_user(user.id)
    return {
        "receipts": [
            {"id": item.id, "date": moscow(item.paid_at), "amount": str(item.amount)}
            for item in items
        ],
        "count": len(items),
    }


@inject
async def on_receipt(
    callback: CallbackQuery,
    widget: Any,
    dialog_manager: DialogManager,
    item_id: str,
    receipts: FromDishka[Receipts],
    notifier: FromDishka[Notifier],
    i18n: FromDishka[TranslatorRunner],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    receipt = await receipts.get(int(item_id))
    # Only one's own, and only one in the cabinet.
    if receipt is None or receipt.user_id != user.id or not receipt.receipt_uuid:
        await notifier.notify_user(user, i18n_key="ntf-s-receipts.none")
        return
    if receipt.inn is None:
        return

    url = receipt_url(receipt.inn, receipt.receipt_uuid)
    file = await receipts.fetch_file(receipt)
    if file is None or callback.message is None:
        await notifier.notify_user(
            user,
            payload=MessagePayloadDto(
                i18n_key="ntf-s-receipts.unavailable",
                i18n_kwargs={"url": url},
                delete_after=None,
            ),
        )
        return
    await callback.message.answer_document(
        BufferedInputFile(file.content, filename=file_name(receipt, file)),
        caption=i18n.get(
            "msg-s-receipt.sent", name=receipt.name, amount=str(receipt.amount), url=url
        ),
    )
    # The list comes back under the file.
    dialog_manager.show_mode = ShowMode.DELETE_AND_SEND


my_receipts = Window(
    Banner(BannerName.SUBSCRIPTION),
    I18nFormat("msg-s-receipt.list"),
    ScrollingGroup(
        Select(
            I18nFormat("btn-s-receipts.file", date=F["item"]["date"], amount=F["item"]["amount"]),
            id="s_receipt",
            item_id_getter=lambda item: item["id"],
            items="receipts",
            on_click=on_receipt,
        ),
        id="s_receipts",
        width=1,
        height=8,
        hide_on_single_page=True,
    ),
    Row(Cancel(I18nFormat("btn-back.general"))),
    IgnoreUpdate(),
    state=SReceipts.MAIN,
    getter=my_receipts_getter,
)

router = Dialog(my_receipts)
