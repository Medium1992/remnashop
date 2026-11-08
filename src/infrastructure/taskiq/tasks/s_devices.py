from typing import Optional

from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.services.s.jobs import AdminAction, DeviceJobs, ReminderKind
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


@broker.task(retry_on_error=False)
@inject(patch_module=True)
async def s_refresh_client_channel_task(
    app: str,
    channel: str,
    jobs: FromDishka[DeviceJobs],
) -> None:
    await jobs.refresh_channel(app, channel)


@broker.task(retry_on_error=False)
@inject(patch_module=True)
async def s_device_admin_task(
    admin_telegram_id: int,
    kind: str,
    action: str,
    target_telegram_id: Optional[int],
    jobs: FromDishka[DeviceJobs],
) -> None:
    await jobs.admin_action(
        admin_telegram_id, ReminderKind(kind), AdminAction(action), target_telegram_id
    )
