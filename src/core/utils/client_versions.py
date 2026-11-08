"""Client app versions: where "latest" comes from, and who is behind.

Pure logic only -- fetching lives in ``infrastructure/services/client_versions``.

Facts this encodes (verified against live data by the access bot it came from):

* User-Agent carries everything: ``Happ/4.1.0/Android/<build>``,
  ``INCY/3.5.5/android Dalvik/2.1.0``, ``Happ/5.6.0/ios/<build>``.
* Happ ships two parallel iOS lineages -- 5.x in the global App Store and 4.x in
  the Russian one -- so a channel can be pinned to a major version.
* macOS Happ is the Catalyst build of the iOS app, so it follows iOS channels.
* INCY's Android version is NOT the release tag (tag ``desktop-v3.7.0`` carries
  an APK whose versionName is 3.5.7), so it is read from the APK manifest.
* The Google Play listing shows the last fully rolled-out release, which lags a
  staged rollout; such a channel is informational and never the comparison.
"""

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Optional

ChannelConfig = dict[str, Any]
Sources = dict[str, dict[str, Any]]

ROUTER_PLATFORM = "router"

DEFAULT_SOURCES: Sources = {
    "happ": {
        "title": "Happ",
        "channels": {
            "android": {
                "label": "Android · APK GitHub",
                "platforms": ["android"],
                "type": "github",
                "repo": "Happ-proxy/happ-android",
                "url": "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk",
                "user_hint": "sideload",
                "priority": 1,
                "enabled": True,
            },
            "android_play": {
                "label": "Android · Google Play",
                "platforms": ["android"],
                "type": "play",
                "package": "com.happproxy",
                "informational": True,
                "hl": "ru",
                "gl": "RU",
                "url": "https://play.google.com/store/apps/details?id=com.happproxy",
                "priority": 2,
                "enabled": True,
            },
            "ios_global": {
                "label": "iOS/macOS · App Store (Global)",
                "platforms": ["ios", "macos"],
                "major": 5,
                "type": "appstore",
                "app_id": 6504287215,
                "countries": ["us", "de", "tr"],
                "url": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
                "priority": 1,
                "enabled": True,
            },
            "ios_ru": {
                "label": "iOS/macOS · App Store (RU)",
                "platforms": ["ios", "macos"],
                "major": 4,
                "type": "appstore",
                "app_id": 6788279553,
                "countries": ["ru"],
                "url": "https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6788279553",
                # The RU listing gets pulled regularly; fall back to the newest
                # version actually seen among our own users.
                "fallback": "observed",
                "priority": 1,
                "enabled": True,
            },
            "desktop": {
                "label": "Windows/Linux · GitHub",
                "platforms": ["windows", "linux"],
                "type": "github",
                "repo": "Happ-proxy/happ-desktop",
                "url": "https://github.com/Happ-proxy/happ-desktop/releases/latest",
                "priority": 1,
                "enabled": True,
            },
        },
    },
    "incy": {
        "title": "INCY",
        "channels": {
            "android": {
                "label": "Android · APK GitHub",
                "platforms": ["android"],
                "type": "github_apk",
                "repo": "INCY-DEV/incy-platforms",
                "asset": "Incy.apk",
                "url": "https://github.com/INCY-DEV/incy-platforms/releases/latest/download/Incy.apk",
                "user_hint": "sideload",
                "priority": 1,
                "enabled": True,
            },
            "android_play": {
                "label": "Android · Google Play",
                "platforms": ["android"],
                "type": "play",
                "package": "llc.itdev.incy",
                "informational": True,
                "hl": "ru",
                "gl": "RU",
                "url": "https://play.google.com/store/apps/details?id=llc.itdev.incy",
                "priority": 2,
                "enabled": True,
            },
            "ios": {
                "label": "iOS · App Store",
                "platforms": ["ios"],
                "type": "appstore",
                "app_id": 6756943388,
                "countries": ["ru", "us"],
                "url": "https://apps.apple.com/us/app/incy/id6756943388",
                "priority": 1,
                "enabled": True,
            },
            "desktop": {
                "label": "Windows/macOS/Linux · GitHub",
                "platforms": ["windows", "macos", "linux"],
                "type": "github",
                "repo": "INCY-DEV/incy-platforms",
                "strip_prefix": "desktop-v",
                "url": "https://github.com/INCY-DEV/incy-platforms/releases/latest",
                "priority": 1,
                "enabled": True,
            },
        },
    },
}

# Keys an admin may change on a channel from the dashboard.
EDITABLE_KEYS = frozenset(
    {"type", "repo", "asset", "url", "app_id", "countries", "package", "version", "enabled"}
)


# ── user agent ───────────────────────────────────────────────────────────────

_UA_RE = re.compile(r"^(Happ|INCY)/(\d+(?:\.\d+)*)(?:/([A-Za-z0-9 ]+))?", re.I)

_PLATFORM_HINTS = (
    ("android", "android"),
    ("ios", "ios"),
    ("macos", "macos"),
    ("mac os", "macos"),
    ("windows", "windows"),
    ("linux", "linux"),
)


@dataclass(frozen=True)
class ClientInfo:
    app: str
    version: str
    platform: Optional[str]


def _platform_from(text: str) -> Optional[str]:
    low = (text or "").strip().lower()
    for needle, name in _PLATFORM_HINTS:
        if low.startswith(needle):
            return name
    return None


def parse_client(
    user_agent: Optional[str], device_os: Optional[str] = None
) -> Optional[ClientInfo]:
    """App, version and platform from a HWID record, or None for other clients."""
    m = _UA_RE.match((user_agent or "").strip())
    if not m:
        return None
    platform = _platform_from(m.group(3) or "") or _platform_from(device_os or "")
    if (device_os or "").lower().startswith("routeros"):
        # Happ on MikroTik: no app store, nothing to recommend.
        platform = ROUTER_PLATFORM
    return ClientInfo(app=m.group(1).lower(), version=m.group(2), platform=platform)


def app_title(app: str) -> str:
    return str(DEFAULT_SOURCES.get(app, {}).get("title") or app)


# ── versions ─────────────────────────────────────────────────────────────────


def version_tuple(version: Optional[str]) -> tuple[int, ...]:
    parts = []
    for chunk in str(version or "").split("."):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts) or (0,)


def _padded(left: str, right: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    a, b = version_tuple(left), version_tuple(right)
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)), b + (0,) * (size - len(b))


def compare(left: str, right: str) -> int:
    a, b = _padded(left, right)
    return (a > b) - (a < b)


def notify_worthy(installed: str, latest: str, *, ignore_patch: bool, min_minor_gap: int) -> bool:
    """Is this gap worth telling a person about?

    Patch-only differences are skipped by default: on the access bot's real
    data that was the difference between pinging 17 people and pinging 160.
    """
    if compare(latest, installed) <= 0:
        return False
    a, b = _padded(installed, latest)
    if a[0] != b[0]:
        return True
    minor_gap = (b[1] if len(b) > 1 else 0) - (a[1] if len(a) > 1 else 0)
    if minor_gap >= min_minor_gap:
        return True
    return not ignore_patch


# ── sources ──────────────────────────────────────────────────────────────────


def merge_sources(overrides: dict[str, dict[str, dict[str, Any]]]) -> Sources:
    """Built-in sources with the admin's edits on top.

    Only known apps and channels are kept: an override for a channel that no
    longer exists is ignored rather than resurrected.
    """
    merged = copy.deepcopy(DEFAULT_SOURCES)
    for app, conf in merged.items():
        app_overrides = overrides.get(app) or {}
        for key, channel in conf["channels"].items():
            patch = app_overrides.get(key)
            if isinstance(patch, dict):
                channel.update({k: v for k, v in patch.items() if k in EDITABLE_KEYS})
    return merged


def channels_for(
    app: str, platform: Optional[str], version: str, sources: Sources
) -> list[tuple[str, ChannelConfig]]:
    """Enabled channels that can serve this install, best first."""
    app_conf = sources.get(app)
    if not app_conf or not platform:
        return []
    major = version_tuple(version)[0]
    matches = []
    for key, channel in app_conf["channels"].items():
        if not channel.get("enabled", True):
            continue
        if platform not in channel.get("platforms", []):
            continue
        pinned = channel.get("major")
        if pinned is not None and int(pinned) != major:
            continue
        matches.append((int(channel.get("priority", 9)), key, channel))
    matches.sort(key=lambda item: item[0])
    return [(key, channel) for _, key, channel in matches]


# ── cache entries ────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class ChannelState:
    """What a channel reported on its last check."""

    version: str = ""
    published: str = ""
    # Human text for channels that publish no comparable number (Google Play).
    display: str = ""
    source: str = ""
    # ok | error | fallback | disabled | unset
    status: str = ""
    checked_at: float = 0.0


@dataclass(kw_only=True)
class VersionsCache:
    channels: dict[str, ChannelState] = field(default_factory=dict)
    # "<app>/<platform>/<major>" -> newest version trusted from our own devices
    observed: dict[str, str] = field(default_factory=dict)
    observed_at: float = 0.0
    checked_at: float = 0.0

    def channel(self, app: str, key: str) -> ChannelState:
        return self.channels.get(f"{app}/{key}") or ChannelState()


def observed_versions(clients: list[ClientInfo], min_devices: int) -> dict[str, str]:
    """Newest version actually seen per app/platform/major.

    The safety net for channels with no public API -- the pulled RU listing of
    Happ, for instance. A version is only trusted once ``min_devices`` report
    it, so a single beta tester cannot move the bar; with nobody past the bar
    the newest seen version is used.
    """
    counts: dict[str, dict[str, int]] = {}
    for info in clients:
        if not info.platform or info.platform == ROUTER_PLATFORM:
            continue
        key = f"{info.app}/{info.platform}/{version_tuple(info.version)[0]}"
        bucket = counts.setdefault(key, {})
        bucket[info.version] = bucket.get(info.version, 0) + 1

    observed = {}
    for key, versions in counts.items():
        trusted = [v for v, n in versions.items() if n >= min_devices]
        observed[key] = max(trusted or list(versions), key=version_tuple)
    return observed


def observed_for(cache: VersionsCache, app: str, channel: ChannelConfig) -> Optional[str]:
    major = channel.get("major")
    best: Optional[str] = None
    for platform in channel.get("platforms", []):
        candidate: Optional[str] = None
        if major is not None:
            candidate = cache.observed.get(f"{app}/{platform}/{major}")
        else:
            prefix = f"{app}/{platform}/"
            for key, value in cache.observed.items():
                if key.startswith(prefix) and (candidate is None or compare(value, candidate) > 0):
                    candidate = value
        if candidate and (best is None or compare(candidate, best) > 0):
            best = candidate
    return best


# ── recommendation ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChannelOffer:
    channel: str
    label: str
    url: str
    # "sideload": installing this build is a channel switch, not an upgrade.
    hint: str
    latest: str
    display: str
    published: str
    behind: bool


@dataclass(frozen=True)
class InstallVerdict:
    app: str
    platform: str
    installed: str
    primary: ChannelOffer
    alternatives: tuple[ChannelOffer, ...]

    @property
    def outdated(self) -> bool:
        return self.primary.behind


def check_install(
    info: ClientInfo, cache: VersionsCache, sources: Sources
) -> Optional[InstallVerdict]:
    """What to tell a user about one installed client.

    None when there is nothing to say: an unknown app, a router, or no channel
    with a usable version.
    """
    if not info.platform or info.platform == ROUTER_PLATFORM:
        return None

    primary: Optional[ChannelOffer] = None
    alternatives: list[ChannelOffer] = []
    for key, channel in channels_for(info.app, info.platform, info.version, sources):
        state = cache.channel(info.app, key)
        latest = state.version
        if not latest and not channel.get("informational"):
            continue
        offer = ChannelOffer(
            channel=key,
            label=str(channel.get("label", key)),
            url=str(channel.get("url", "")),
            hint=str(channel.get("user_hint", "")),
            latest=latest,
            display=state.display,
            published=state.published,
            behind=bool(latest) and compare(latest, info.version) > 0,
        )
        if primary is None and latest:
            primary = offer
        else:
            alternatives.append(offer)

    if primary is None:
        return None
    return InstallVerdict(
        app=info.app,
        platform=info.platform,
        installed=info.version,
        primary=primary,
        alternatives=tuple(alternatives),
    )
