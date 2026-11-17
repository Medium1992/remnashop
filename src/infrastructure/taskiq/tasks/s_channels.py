from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.services.s.channels import GatewayChannels
from src.infrastructure.taskiq.broker import broker


# Tells the owner once that the bot now offers Stars only; a no-op afterwards.
@broker.task(schedule=[{"cron": "*/10 * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_channels_notice_task(channels: FromDishka[GatewayChannels]) -> None:
    await channels.tell_once()
