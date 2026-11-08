"""Refreshing the cached versions: what a failed source leaves behind."""

from typing import Dict, Optional

import httpx

from src.application.dto.s_settings import ClientVersionsSettingsDto
from src.core.utils.client_versions import ChannelState, ClientInfo
from src.infrastructure.services.s.client_versions import ClientVersionsService


class FakeRedis:
    def __init__(self) -> None:
        self.data: Dict[str, str] = {}

    async def get(self, name: str) -> Optional[str]:
        return self.data.get(name)

    async def set(self, name: str, value: str) -> bool:
        self.data[name] = value
        return True


def only_manual(**extra: object) -> ClientVersionsSettingsDto:
    """Every channel off except the ones named, so no test reaches the network."""
    sources: dict = {
        app: {key: {"enabled": False} for key in keys}
        for app, keys in {
            "happ": ["android", "android_play", "ios_global", "ios_ru", "desktop"],
            "incy": ["android", "android_play", "ios", "desktop"],
        }.items()
    }
    for name, patch in extra.items():
        app, _, key = name.partition("__")
        sources[app][key] = patch
    return ClientVersionsSettingsDto(sources=sources)


def offline() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))


class TestRefresh:
    async def test_a_manual_version_is_published(self) -> None:
        service = ClientVersionsService(FakeRedis())  # type: ignore[arg-type]
        settings = only_manual(
            happ__desktop={"enabled": True, "type": "manual", "version": "2.0.0"}
        )

        async with offline() as http:
            cache = await service.refresh(settings, http=http)

        assert cache.channel("happ", "desktop").version == "2.0.0"
        assert (await service.load()).channel("happ", "desktop").version == "2.0.0"

    async def test_a_failed_source_keeps_its_last_version(self) -> None:
        redis = FakeRedis()
        service = ClientVersionsService(redis)  # type: ignore[arg-type]
        settings = only_manual(happ__android={"enabled": True})
        async with offline() as http:
            first = await service.refresh(settings, http=http)
            first.channels["happ/android"] = ChannelState(version="4.3.0", status="ok")
            await service._save(first)

            cache = await service.refresh(settings, http=http, force=True)

        state = cache.channel("happ", "android")
        assert state.version == "4.3.0"
        assert state.status == "error"

    async def test_a_pulled_listing_falls_back_to_what_users_run(self) -> None:
        service = ClientVersionsService(FakeRedis())  # type: ignore[arg-type]
        settings = only_manual(happ__ios_ru={"enabled": True})
        clients = [ClientInfo("happ", "4.2.0", "ios")] * 2

        async with offline() as http:
            cache = await service.refresh(settings, clients=clients, http=http)

        state = cache.channel("happ", "ios_ru")
        assert (state.version, state.status, state.source) == ("4.2.0", "fallback", "observed")

    async def test_a_fresh_entry_is_not_fetched_again_before_the_interval(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"tag_name": "v4.3.0"})

        service = ClientVersionsService(FakeRedis())  # type: ignore[arg-type]
        settings = only_manual(happ__android={"enabled": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await service.refresh(settings, http=http)
            await service.refresh(settings, http=http)
            assert len(calls) == 1
            await service.refresh(settings, http=http, force=True)
            assert len(calls) == 2

    async def test_an_unreadable_cache_starts_over(self) -> None:
        redis = FakeRedis()
        service = ClientVersionsService(redis)  # type: ignore[arg-type]
        redis.data[service._key] = "{not json"

        assert (await service.load()).channels == {}
