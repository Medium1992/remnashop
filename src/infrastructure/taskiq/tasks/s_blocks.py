from dishka.integrations.taskiq import FromDishka, inject

from src.application.common.interactor import SYSTEM_ACTOR
from src.infrastructure.services.s.blocks import BlockService
from src.infrastructure.taskiq.broker import broker


# Every minute: a one-hour block should not run noticeably longer.
@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_release_blocks_task(blocks: FromDishka[BlockService]) -> None:
    await blocks.release_due(SYSTEM_ACTOR)
