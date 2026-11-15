from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.services.s.receipts import Receipts
from src.infrastructure.taskiq.broker import broker


# Every minute: receipts for payments the purchase event missed, retries, refunds.
@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_receipts_task(receipts: FromDishka[Receipts]) -> None:
    await receipts.run()
