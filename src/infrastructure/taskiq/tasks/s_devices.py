from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.services.s.jobs import DeviceJobs
from src.infrastructure.taskiq.broker import broker


# Hourly, but each source is only fetched once its refresh interval is up.
@broker.task(schedule=[{"cron": "15 * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_refresh_client_versions_task(jobs: FromDishka[DeviceJobs]) -> None:
    await jobs.refresh_versions()


@broker.task(retry_on_error=False)
@inject(patch_module=True)
async def s_force_refresh_client_versions_task(jobs: FromDishka[DeviceJobs]) -> None:
    await jobs.refresh_versions(force=True)


# Once a day, at 12:00 Moscow time: reminders are not something to get at night.
@broker.task(schedule=[{"cron": "0 9 * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def s_device_reminders_task(jobs: FromDishka[DeviceJobs]) -> None:
    await jobs.send_reminders()
