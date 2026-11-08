"""The version sources, against recorded shapes of what each one answers."""

import io
import json
import zipfile
from typing import Callable

import httpx
import pytest

from src.infrastructure.services.s.version_fetchers import (
    fetch_appstore,
    fetch_channel,
    fetch_github_release,
    fetch_play,
    parse_play_page,
    read_apk_version,
)

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def manifest_with(version: str) -> bytes:
    """A stand-in for binary XML: strings stored as UTF-16LE among other bytes."""
    return (
        b"\x03\x00\x08\x00"
        + "android".encode("utf-16-le")
        + b"\x00\x00\x01"
        + version.encode("utf-16-le")
        + b"\x00\x00"
        + "com.example".encode("utf-16-le")
    )


def an_apk(version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as apk:
        apk.writestr("classes.dex", b"\x00" * 5000)
        apk.writestr("AndroidManifest.xml", manifest_with(version))
        apk.writestr("resources.arsc", b"\x01" * 3000)
    return buffer.getvalue()


def serving(blob: bytes, *, honour_range: bool = True) -> tuple[Handler, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(blob))})
        spec = request.headers.get("range", "")
        if honour_range and spec.startswith("bytes="):
            start, end = (int(x) for x in spec[6:].split("-"))
            return httpx.Response(206, content=blob[start : end + 1])
        return httpx.Response(200, content=blob)

    return handler, seen


class TestApk:
    async def test_the_version_is_read_from_the_manifest(self) -> None:
        handler, _ = serving(an_apk("3.5.7"))
        async with client(handler) as c:
            assert await read_apk_version(c, "https://x/Incy.apk") == "3.5.7"

    async def test_a_server_ignoring_range_still_works(self) -> None:
        handler, _ = serving(an_apk("3.5.7"), honour_range=False)
        async with client(handler) as c:
            assert await read_apk_version(c, "https://x/Incy.apk") == "3.5.7"

    async def test_not_a_zip_is_unknown_not_an_error(self) -> None:
        handler, _ = serving(b"<html>not found</html>" * 10)
        async with client(handler) as c:
            assert await read_apk_version(c, "https://x/Incy.apk") is None


class TestGithub:
    async def test_the_release_tag_is_the_version(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"tag_name": "desktop-v3.7.0", "published_at": "2026-10-01T10:00:00Z"}
            )

        async with client(handler) as c:
            state = await fetch_github_release(c, "INCY-DEV/incy-platforms", "desktop-v")

        assert state is not None
        assert (state.version, state.published) == ("3.7.0", "2026-10-01")

    async def test_a_403_from_the_api_falls_back_to_the_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.github.com":
                return httpx.Response(403, json={"message": "rate limited"})
            return httpx.Response(
                302,
                headers={
                    "location": "https://github.com/Happ-proxy/happ-android/releases/tag/v4.3.0"
                },
            )

        async with client(handler) as c:
            state = await fetch_github_release(c, "Happ-proxy/happ-android")

        assert state is not None and state.version == "4.3.0"

    async def test_a_tag_that_is_not_a_version_is_unknown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.github.com":
                return httpx.Response(200, json={"tag_name": "nightly"})
            return httpx.Response(404)

        async with client(handler) as c:
            assert await fetch_github_release(c, "a/b") is None


class TestAppStore:
    async def test_the_first_storefront_carrying_the_app_wins(self) -> None:
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            country = request.url.params["country"]
            asked.append(country)
            if country == "us":
                return httpx.Response(200, json={"resultCount": 0, "results": []})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"version": "5.6.0", "currentVersionReleaseDate": "2026-10-20T07:00:00Z"}
                    ]
                },
            )

        async with client(handler) as c:
            state = await fetch_appstore(c, 6504287215, ["us", "de", "tr"])

        assert asked == ["us", "de"]
        assert state is not None and (state.version, state.published) == ("5.6.0", "2026-10-20")

    async def test_pulled_everywhere_is_unknown(self) -> None:
        async with client(lambda r: httpx.Response(200, json={"results": []})) as c:
            assert await fetch_appstore(c, 1, ["ru"]) is None


def a_play_page(version: str, updated: str) -> str:
    blob: list = [None, [None, None, [None] * 146]]
    blob[1][2][140] = [[[version]]]
    blob[1][2][145] = [[updated]]
    review = "AF_initDataCallback({key: 'ds:3', data:[\"4.1.0\"], sideChannel: {}});"
    listing = f"AF_initDataCallback({{key: 'ds:5', data:{json.dumps(blob)}, sideChannel: {{}}}});"
    return f"<html><script>{review}</script><script>{listing}</script></html>"


class TestPlay:
    def test_the_listing_blob_is_read_not_review_text(self) -> None:
        assert parse_play_page(a_play_page("3.26.3", "1 нояб. 2026 г.")) == (
            "3.26.3",
            "1 нояб. 2026 г.",
        )

    async def test_the_channel_publishes_the_date_and_no_comparable_version(self) -> None:
        page = a_play_page("3.26.3", "1 нояб. 2026 г.")
        async with client(lambda r: httpx.Response(200, text=page)) as c:
            state = await fetch_play(c, "com.happproxy")

        assert state is not None
        assert state.version == ""
        assert state.display == "1 нояб. 2026 г."


class TestManual:
    @pytest.mark.parametrize("version, expected", [("4.4.0", "4.4.0"), ("", None), (" ", None)])
    async def test_manual(self, version: str, expected: object) -> None:
        async with client(lambda r: httpx.Response(500)) as c:
            state = await fetch_channel(c, {"type": "manual", "version": version})
        assert (state.version if state else None) == expected
