from typing import Final, Optional

import orjson
from adaptix import Retort
from dishka.integrations.taskiq import FromDishka, inject
from loguru import logger
from packaging.version import InvalidVersion, Version
from redis.asyncio import Redis

from src.application.common import EventPublisher
from src.application.events import BotUpdateEvent
from src.core.config import AppConfig
from src.infrastructure.redis.keys import LatestNotifiedVersionKey
from src.infrastructure.taskiq.broker import broker

GITHUB_RELEASE_URL: Final[str] = "https://api.github.com/repos/snoups/remnashop/releases/latest"


def parse_release_version(tag: Optional[str]) -> Optional[Version]:
    """A release tag as a comparable version, or None if it is not one.

    Builds do not always carry a release number: a fork or a branch build may be
    tagged with a name instead. That is not an error worth an alert every hour,
    it just means there is nothing to compare against.

    Only a leading ``v`` is dropped. A local label such as ``0.8.2+3x`` is kept:
    it sorts just above the release it is built on, so the next upstream release
    still registers as newer.
    """
    if not tag:
        return None

    try:
        return Version(tag.strip().removeprefix("v"))
    except InvalidVersion:
        return None


@broker.task(schedule=[{"cron": "0 * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def check_bot_update(
    config: FromDishka[AppConfig],
    retort: FromDishka[Retort],
    redis: FromDishka[Redis],
    event_publisher: FromDishka[EventPublisher],
) -> None:
    if not config.build.tag or config.build.tag == "dev":
        logger.debug("Local version is a development build, skipping update check")
        return

    lv = parse_release_version(config.build.tag)

    if lv is None:
        logger.debug(
            f"Build tag '{config.build.tag}' is not a release version, skipping update check"
        )
        return

    local_version = str(lv)

    import httpx  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=30.0,
                write=10.0,
                pool=5.0,
            )
        ) as client:
            headers = {"Accept": "application/vnd.github.v3+json"}
            response = await client.get(GITHUB_RELEASE_URL, headers=headers)
            response.raise_for_status()

            data = orjson.loads(response.content)
            rv = parse_release_version(data.get("tag_name"))

            if rv is None:
                logger.error("Remote version tag not found in GitHub API response")
                return

            remote_version = str(rv)
    except httpx.ConnectError as e:
        logger.warning(f"Failed to reach GitHub API (network issue): '{e}'")
        return
    except httpx.TimeoutException as e:
        logger.warning(f"GitHub API request timed out: '{e}'")
        return
    except httpx.HTTPStatusError as e:
        logger.warning(f"GitHub API returned error status: '{e}'")
        return

    if rv <= lv:
        status = "up to date" if rv == lv else "ahead of remote"
        logger.debug(f"Project is '{status}': '{local_version}'")
        return

    key = retort.dump(LatestNotifiedVersionKey(version="*"))
    last_notified_version = await redis.get(key)

    logger.debug(
        f"Update check: key='{key}', cached={last_notified_version!r}, remote={remote_version!r}"
    )

    if last_notified_version == remote_version:
        logger.debug(f"Version '{remote_version}' already notified")
        return

    await redis.set(key, value=remote_version)
    logger.info(f"New version available: '{remote_version}' (local: '{local_version}')")

    event = BotUpdateEvent(local_version=local_version, remote_version=remote_version)
    await event_publisher.publish(event)
