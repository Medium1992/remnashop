from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.services.s.slots import SlotService
from src.infrastructure.taskiq.broker import broker


# Every minute: a temporary slot lives a few minutes, and an unused one must
# not stay open much longer than it promised.
@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_slots_close_expired_task(slots: FromDishka[SlotService]) -> None:
    await slots.close_expired()


# The device webhooks keep limits right as devices come and go; this catches
# whatever a missed webhook left behind.
@broker.task(schedule=[{"cron": "40 * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_slots_sweep_task(slots: FromDishka[SlotService]) -> None:
    await slots.sweep()
