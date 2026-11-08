"""Where the latest Happ / INCY versions are read from.

Each fetcher returns a fresh ``ChannelState`` or None when the source did not
answer usefully. None is "unknown", never "no update": the caller keeps the
previous value and may fall back to what users' own devices report.
"""

import json
import re
import struct
import zlib
from typing import Any, Optional

import httpx
from loguru import logger

from src.core.utils.client_versions import ChannelConfig, ChannelState, version_tuple

GITHUB_API = "https://api.github.com"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
PLAY_DETAILS = "https://play.google.com/store/apps/details"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _clean_tag(tag: Optional[str], strip_prefix: str = "") -> Optional[str]:
    tag = str(tag or "")
    if strip_prefix and tag.startswith(strip_prefix):
        tag = tag[len(strip_prefix) :]
    tag = tag.lstrip("vV")
    return tag if re.match(r"^\d", tag) else None


async def _github_tag_via_redirect(
    client: httpx.AsyncClient, repo: str, strip_prefix: str
) -> Optional[ChannelState]:
    """/releases/latest redirects to /releases/tag/<tag> -- no API, no rate limit.

    The API is the better source (it also carries the date), but it answers 403
    for unauthenticated callers from some networks, and this endpoint does not.
    """
    try:
        response = await client.get(
            f"https://github.com/{repo}/releases/latest", follow_redirects=False
        )
    except httpx.HTTPError as e:
        logger.warning(f"GitHub redirect for '{repo}' failed: {e}")
        return None
    location = response.headers.get("location") or ""
    tag = _clean_tag(location.rsplit("/tag/", 1)[-1] if "/tag/" in location else "", strip_prefix)
    if not tag:
        return None
    return ChannelState(version=tag, source=f"github-redirect:{repo}")


async def fetch_github_release(
    client: httpx.AsyncClient, repo: str, strip_prefix: str = "", token: str = ""
) -> Optional[ChannelState]:
    """The latest non-prerelease tag as the version."""
    if not repo:
        return None
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = await client.get(f"{GITHUB_API}/repos/{repo}/releases/latest", headers=headers)
        if response.status_code == 200:
            body = response.json()
            tag = _clean_tag(body.get("tag_name"), strip_prefix)
            if tag:
                return ChannelState(
                    version=tag,
                    published=str(body.get("published_at") or "")[:10],
                    source=f"github:{repo}",
                )
        else:
            logger.info(f"GitHub API for '{repo}' answered {response.status_code}, trying redirect")
    except (httpx.HTTPError, ValueError) as e:
        logger.info(f"GitHub API for '{repo}' failed ({e}), trying redirect")

    return await _github_tag_via_redirect(client, repo, strip_prefix)


def _manifest_version(manifest: bytes) -> Optional[str]:
    # Binary XML keeps strings as UTF-16LE; versionName looks like 1.2.3.
    found = []
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){3,}", manifest):
        text = match.group().decode("utf-16-le", "ignore")
        if re.fullmatch(r"\d+\.\d+(\.\d+)*", text):
            found.append(text)
    # The manifest holds one version-like literal in practice; take the max.
    return max(found, key=version_tuple) if found else None


async def _ranged(client: httpx.AsyncClient, url: str, start: int, end: int) -> Optional[bytes]:
    response = await client.get(
        url, headers={"Range": f"bytes={start}-{end}"}, follow_redirects=True
    )
    if response.status_code not in (200, 206):
        return None
    if response.status_code == 200:
        # A server that ignores Range sends the whole file; cut out the slice.
        return response.content[start : end + 1]
    return response.content


async def read_apk_version(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """versionName from a remote APK, fetching only the manifest.

    INCY publishes the Android build inside a release tagged for desktop, so the
    tag says 3.7.0 while the APK is 3.5.7. Range requests keep this to a few
    dozen KB instead of the full 55 MB download.
    """
    try:
        head = await client.head(url, follow_redirects=True)
        size = int(head.headers.get("content-length") or 0)
        if not size:
            return None

        tail_len = min(size, 65_536)
        tail = await _ranged(client, url, size - tail_len, size - 1)
        if tail is None:
            return None
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0:
            return None
        cd_size, cd_offset = struct.unpack("<II", tail[eocd + 12 : eocd + 20])

        entries = await _ranged(client, url, cd_offset, cd_offset + cd_size - 1)
        if entries is None:
            return None

        local_offset = comp_size = method = None
        pos = entries.find(b"PK\x01\x02")
        while pos >= 0:
            name_len, extra_len, comment_len = struct.unpack("<HHH", entries[pos + 28 : pos + 34])
            if entries[pos + 46 : pos + 46 + name_len] == b"AndroidManifest.xml":
                method = struct.unpack("<H", entries[pos + 10 : pos + 12])[0]
                comp_size = struct.unpack("<I", entries[pos + 20 : pos + 24])[0]
                local_offset = struct.unpack("<I", entries[pos + 42 : pos + 46])[0]
                break
            pos = entries.find(b"PK\x01\x02", pos + 46 + name_len + extra_len + comment_len)

        if local_offset is None or comp_size is None:
            return None

        raw = await _ranged(client, url, local_offset, local_offset + 30 + comp_size + 4096)
        if raw is None:
            return None
        name_len, extra_len = struct.unpack("<HH", raw[26:30])
        start = 30 + name_len + extra_len
        payload = raw[start : start + comp_size]
        manifest = zlib.decompress(payload, -15) if method == 8 else payload
        return _manifest_version(manifest)
    except (httpx.HTTPError, struct.error, zlib.error, ValueError) as e:
        logger.warning(f"APK manifest read from '{url}' failed: {e}")
        return None


async def fetch_github_apk(
    client: httpx.AsyncClient, repo: str, url: str, token: str = ""
) -> Optional[ChannelState]:
    version = await read_apk_version(client, url)
    if not version:
        return None
    release = await fetch_github_release(client, repo, token=token)
    return ChannelState(
        version=version,
        published=release.published if release else "",
        source=f"apk:{repo}",
    )


async def fetch_appstore(
    client: httpx.AsyncClient, app_id: Any, countries: Optional[list[str]]
) -> Optional[ChannelState]:
    """The first storefront that actually carries the app wins."""
    if not app_id:
        return None
    for country in countries or ["us"]:
        try:
            response = await client.get(
                ITUNES_LOOKUP, params={"id": int(app_id), "country": country}
            )
            if response.status_code != 200:
                continue
            results = (response.json() or {}).get("results") or []
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"App Store lookup '{app_id}/{country}' failed: {e}")
            continue
        for item in results:
            version = item.get("version")
            if version:
                return ChannelState(
                    version=str(version),
                    published=str(item.get("currentVersionReleaseDate") or "")[:10],
                    source=f"appstore:{app_id}/{country}",
                )
    logger.info(f"App Store app '{app_id}' is not available in {countries}")
    return None


_PLAY_BLOB_RE = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
_PLAY_KEY_RE = re.compile(r"key:\s*'([^']+)'")
_PLAY_DATA_RE = re.compile(r"data:(\[.*?\])\s*,\s*sideChannel", re.S)
# Where the page keeps the listed version and the "updated on" date.
_PLAY_VERSION_PATH = (1, 2, 140, 0, 0, 0)
_PLAY_UPDATED_PATH = (1, 2, 145, 0, 0)


def _dig(node: Any, path: tuple[int, ...]) -> Any:
    for step in path:
        try:
            node = node[step]
        except (IndexError, KeyError, TypeError):
            return None
    return node


def parse_play_page(page: str) -> Optional[tuple[str, str]]:
    """(listed version, updated date) from a Google Play details page."""
    for match in _PLAY_BLOB_RE.finditer(page):
        raw = match.group(1)
        key = _PLAY_KEY_RE.search(raw)
        data = _PLAY_DATA_RE.search(raw)
        if not key or not data or key.group(1) != "ds:5":
            continue
        try:
            blob = json.loads(data.group(1))
        except ValueError:
            continue
        version = _dig(blob, _PLAY_VERSION_PATH)
        if not isinstance(version, str) or not re.match(r"^\d", version):
            continue
        updated = _dig(blob, _PLAY_UPDATED_PATH)
        return version, updated if isinstance(updated, str) else ""
    return None


async def fetch_play(
    client: httpx.AsyncClient, package: str, hl: str = "ru", gl: str = "RU"
) -> Optional[ChannelState]:
    """The Google Play listing, as information only.

    Play has no API, so this reads the page's own data blob at a fixed path
    rather than regexing loose version strings -- the page is full of them, and
    the obvious ones belong to user reviews. The listed version is that of the
    last fully rolled-out release: during a staged rollout people already get a
    newer build. So the date is published as the channel's value and the
    number is kept out of every comparison.
    """
    if not package:
        return None
    try:
        response = await client.get(
            PLAY_DETAILS,
            params={"id": package, "hl": hl, "gl": gl},
            headers={"User-Agent": _BROWSER_UA},
        )
    except httpx.HTTPError as e:
        logger.warning(f"Google Play page for '{package}' failed: {e}")
        return None
    if response.status_code != 200:
        logger.warning(f"Google Play page for '{package}' answered {response.status_code}")
        return None

    parsed = parse_play_page(response.text)
    if parsed is None:
        logger.warning(f"Google Play page for '{package}' carries no version")
        return None
    _, updated = parsed
    return ChannelState(display=updated, published=updated, source=f"play:{package}/{gl}")


async def fetch_channel(
    client: httpx.AsyncClient, channel: ChannelConfig, github_token: str = ""
) -> Optional[ChannelState]:
    kind = channel.get("type")
    if kind == "github":
        return await fetch_github_release(
            client, str(channel.get("repo", "")), str(channel.get("strip_prefix", "")), github_token
        )
    if kind == "github_apk":
        return await fetch_github_apk(
            client, str(channel.get("repo", "")), str(channel.get("url", "")), github_token
        )
    if kind == "appstore":
        return await fetch_appstore(client, channel.get("app_id"), channel.get("countries"))
    if kind == "play":
        return await fetch_play(
            client,
            str(channel.get("package", "")),
            str(channel.get("hl", "ru")),
            str(channel.get("gl", "RU")),
        )
    if kind == "manual":
        version = str(channel.get("version") or "").strip()
        return ChannelState(version=version, source="manual") if version else None
    logger.warning(f"Unknown client version channel type '{kind}'")
    return None
