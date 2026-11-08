import json
import os
import time
from typing import Optional

import httpx
from adaptix import Retort
from loguru import logger
from redis.asyncio import Redis

from src.application.dto.s_settings import ClientVersionsSettingsDto
from src.core.utils.client_versions import (
    ChannelConfig,
    ChannelState,
    ClientInfo,
    InstallVerdict,
    Sources,
    VersionsCache,
    check_install,
    merge_sources,
    observed_for,
    observed_versions,
    parse_client,
)
from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import ClientVersionsCacheKey

from .version_fetchers import fetch_channel

# Optional: lifts the GitHub API limit from 60 to 5000 requests an hour.
GITHUB_TOKEN_ENV = "S_GITHUB_TOKEN"
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_USER_AGENT = "remnashop-s"

_retort = Retort()


class ClientVersionsService:
    """The latest Happ / INCY versions, cached in Redis between checks.

    A failed source keeps the version it had: "the store did not answer" is
    not "there is no update". A source with ``fallback: observed`` then reports
    the newest version seen on the users' own devices instead.
    """

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._key = serialize_storage_key(ClientVersionsCacheKey())

    async def load(self) -> VersionsCache:
        raw = await self.redis.get(self._key)
        if not raw:
            return VersionsCache()
        try:
            data = json.loads(raw)
            return _retort.load(data if isinstance(data, dict) else {}, VersionsCache)
        except Exception as e:
            logger.warning(f"Client versions cache unreadable, starting over: {e}")
            return VersionsCache()

    async def _save(self, cache: VersionsCache) -> None:
        await self.redis.set(self._key, json.dumps(_retort.dump(cache)))

    async def refresh(
        self,
        settings: ClientVersionsSettingsDto,
        *,
        clients: Optional[list[ClientInfo]] = None,
        force: bool = False,
        only: Optional[tuple[str, str]] = None,
        http: Optional[httpx.AsyncClient] = None,
    ) -> VersionsCache:
        """Re-check the channels that are due (all of them with ``force``).

        ``clients`` -- the apps on users' devices -- refreshes the observed
        fallback. It is skipped when only one channel is being re-checked.
        """
        sources = merge_sources(settings.sources)
        cache = await self.load()
        now = time.time()
        interval = settings.refresh_hours * 3600

        if clients is not None and only is None:
            cache.observed = observed_versions(clients, settings.observed_min_devices)
            cache.observed_at = now

        owned = http is None
        client = http or httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        token = os.environ.get(GITHUB_TOKEN_ENV, "")
        try:
            for app, app_conf in sources.items():
                for key, channel in app_conf["channels"].items():
                    if only and only != (app, key):
                        continue
                    state = cache.channels.get(f"{app}/{key}")
                    if not channel.get("enabled", True):
                        if state:
                            state.status = "disabled"
                        continue
                    known = state and (state.version or state.display)
                    if state and known and not force and now - state.checked_at < interval:
                        continue
                    fresh = await fetch_channel(client, channel, token)
                    cache.channels[f"{app}/{key}"] = _settle(fresh, state, cache, app, channel, now)
        finally:
            if owned:
                await client.aclose()

        cache.checked_at = now
        await self._save(cache)
        logger.info(f"Client versions refreshed: {summary_line(cache)}")
        return cache

    @staticmethod
    def verdict(
        user_agent: Optional[str],
        platform: Optional[str],
        cache: VersionsCache,
        sources: Sources,
    ) -> Optional[InstallVerdict]:
        info = parse_client(user_agent, platform)
        return check_install(info, cache, sources) if info else None


def _settle(
    fresh: Optional[ChannelState],
    previous: Optional[ChannelState],
    cache: VersionsCache,
    app: str,
    channel: ChannelConfig,
    now: float,
) -> ChannelState:
    """The channel's state after a check: the fresh answer, or what is left."""
    if fresh:
        fresh.status, fresh.checked_at = "ok", now
        return fresh

    state = previous or ChannelState()
    state.checked_at = now
    if channel.get("type") == "manual":
        # Nothing configured yet is not a failure.
        state.version, state.status = "", "unset"
        return state
    state.status = "error"
    if channel.get("fallback") == "observed":
        fallback = observed_for(cache, app, channel)
        if fallback:
            state.version, state.source, state.status = fallback, "observed", "fallback"
    return state


def summary_line(cache: VersionsCache) -> str:
    """One-line digest for the log: what each channel currently says."""
    parts = []
    for key, state in sorted(cache.channels.items()):
        value = state.version or state.display or "-"
        parts.append(f"{key}={value}" + ("" if state.status == "ok" else f"({state.status})"))
    return " ".join(parts) or "no data"
