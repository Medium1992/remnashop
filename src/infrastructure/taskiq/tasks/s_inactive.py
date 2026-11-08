from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.services.s.inactive import InactiveProfiles
from src.infrastructure.taskiq.broker import broker


# Once a day, at 13:00 Moscow time: warnings and deletions of lapsed profiles.
@broker.task(schedule=[{"cron": "0 10 * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_inactive_cleanup_task(inactive: FromDishka[InactiveProfiles]) -> None:
    await inactive.run()
