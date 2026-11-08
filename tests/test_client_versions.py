"""Who is behind on Happ / INCY, and which channel to point them at.

The cases come from the access bot this logic was ported from, where each one
was a real mistake once: the two iOS lineages of Happ, INCY's APK not matching
its release tag, Google Play's listing lagging a staged rollout.
"""

import pytest

from src.core.utils.client_versions import (
    ROUTER_PLATFORM,
    ChannelState,
    ClientInfo,
    VersionsCache,
    channels_for,
    check_install,
    compare,
    merge_sources,
    notify_worthy,
    observed_for,
    observed_versions,
    parse_client,
)


def cache_with(**versions: str) -> VersionsCache:
    """``happ_android="4.3.0"`` -> channel happ/android at 4.3.0."""
    cache = VersionsCache()
    for name, version in versions.items():
        app, _, key = name.partition("_")
        cache.channels[f"{app}/{key}"] = ChannelState(version=version, status="ok")
    return cache


class TestUserAgent:
    @pytest.mark.parametrize(
        "ua, expected",
        [
            ("Happ/4.1.0/Android/1726", ClientInfo("happ", "4.1.0", "android")),
            ("INCY/3.5.5/android Dalvik/2.1.0", ClientInfo("incy", "3.5.5", "android")),
            ("Happ/5.6.0/ios/551", ClientInfo("happ", "5.6.0", "ios")),
            ("happ/2.1.4/Windows", ClientInfo("happ", "2.1.4", "windows")),
        ],
    )
    def test_known_clients_parse(self, ua: str, expected: ClientInfo) -> None:
        assert parse_client(ua) == expected

    @pytest.mark.parametrize("ua", ["v2rayNG/1.8.5", "clash.meta/1.18", "", None])
    def test_other_clients_are_not_ours_to_judge(self, ua: object) -> None:
        assert parse_client(ua) is None  # type: ignore[arg-type]

    def test_the_platform_falls_back_to_the_device_os(self) -> None:
        assert parse_client("Happ/4.1.0", "Android 14") == ClientInfo("happ", "4.1.0", "android")

    def test_happ_on_routeros_is_a_router(self) -> None:
        info = parse_client("Happ/1.0.0/Linux", "RouterOS 7.16")
        assert info is not None and info.platform == ROUTER_PLATFORM


class TestComparison:
    @pytest.mark.parametrize(
        "left, right, result",
        [("3.5.7", "3.5.5", 1), ("3.5", "3.5.0", 0), ("4.10.0", "4.9.9", 1), ("1.0", "1.0.1", -1)],
    )
    def test_numeric_not_lexical(self, left: str, right: str, result: int) -> None:
        assert compare(left, right) == result

    def test_a_patch_gap_is_not_worth_a_message_by_default(self) -> None:
        assert not notify_worthy("3.5.5", "3.5.7", ignore_patch=True, min_minor_gap=1)

    def test_a_patch_gap_counts_when_asked_to(self) -> None:
        assert notify_worthy("3.5.5", "3.5.7", ignore_patch=False, min_minor_gap=1)

    def test_a_minor_or_major_gap_is(self) -> None:
        assert notify_worthy("4.1.0", "4.2.0", ignore_patch=True, min_minor_gap=1)
        assert notify_worthy("4.9.0", "5.0.0", ignore_patch=True, min_minor_gap=1)

    def test_up_to_date_or_ahead_is_not(self) -> None:
        assert not notify_worthy("4.3.0", "4.3.0", ignore_patch=False, min_minor_gap=1)
        assert not notify_worthy("4.4.0", "4.3.0", ignore_patch=False, min_minor_gap=1)


class TestChannels:
    def test_happ_ios_follows_its_own_lineage(self) -> None:
        sources = merge_sources({})

        global_line = [k for k, _ in channels_for("happ", "ios", "5.6.0", sources)]
        russian_line = [k for k, _ in channels_for("happ", "ios", "4.2.0", sources)]

        assert global_line == ["ios_global"]
        assert russian_line == ["ios_ru"]

    def test_macos_happ_follows_ios(self) -> None:
        keys = [k for k, _ in channels_for("happ", "macos", "5.1.0", merge_sources({}))]
        assert keys == ["ios_global"]

    def test_android_prefers_github_over_play(self) -> None:
        keys = [k for k, _ in channels_for("happ", "android", "4.1.0", merge_sources({}))]
        assert keys == ["android", "android_play"]

    def test_a_disabled_channel_is_skipped(self) -> None:
        sources = merge_sources({"happ": {"android": {"enabled": False}}})
        keys = [k for k, _ in channels_for("happ", "android", "4.1.0", sources)]
        assert keys == ["android_play"]

    def test_only_editable_keys_are_taken_from_overrides(self) -> None:
        sources = merge_sources(
            {"happ": {"ios_ru": {"app_id": 1, "platforms": ["windows"]}}, "ghost": {"x": {}}}
        )
        channel = sources["happ"]["channels"]["ios_ru"]
        assert channel["app_id"] == 1
        assert channel["platforms"] == ["ios", "macos"]
        assert "ghost" not in sources


class TestVerdict:
    def test_behind_on_the_primary_channel(self) -> None:
        verdict = check_install(
            ClientInfo("happ", "4.1.0", "android"),
            cache_with(happ_android="4.3.0"),
            merge_sources({}),
        )
        assert verdict is not None
        assert verdict.outdated
        assert verdict.primary.channel == "android"
        assert verdict.primary.hint == "sideload"

    def test_up_to_date(self) -> None:
        verdict = check_install(
            ClientInfo("happ", "4.3.0", "android"),
            cache_with(happ_android="4.3.0"),
            merge_sources({}),
        )
        assert verdict is not None and not verdict.outdated

    def test_play_is_never_the_comparison(self) -> None:
        # The listing said 3.26.3 while people were getting 4.3.0.
        cache = cache_with(happ_android="4.3.0")
        cache.channels["happ/android_play"] = ChannelState(display="обновлено 1 нояб.", status="ok")

        verdict = check_install(ClientInfo("happ", "4.3.0", "android"), cache, merge_sources({}))

        assert verdict is not None
        assert verdict.primary.channel == "android"
        assert [a.channel for a in verdict.alternatives] == ["android_play"]

    def test_no_usable_version_means_nothing_to_say(self) -> None:
        verdict = check_install(
            ClientInfo("happ", "5.0.0", "ios"), VersionsCache(), merge_sources({})
        )
        assert verdict is None

    def test_a_router_gets_no_advice(self) -> None:
        verdict = check_install(
            ClientInfo("happ", "1.0.0", ROUTER_PLATFORM),
            cache_with(happ_desktop="9.9.9"),
            merge_sources({}),
        )
        assert verdict is None


class TestObserved:
    def test_a_single_beta_tester_does_not_move_the_bar(self) -> None:
        clients = [
            ClientInfo("happ", "4.2.0", "ios"),
            ClientInfo("happ", "4.2.0", "ios"),
            ClientInfo("happ", "4.9.0", "ios"),
        ]
        assert observed_versions(clients, min_devices=2) == {"happ/ios/4": "4.2.0"}

    def test_lineages_are_kept_apart(self) -> None:
        clients = [ClientInfo("happ", "4.2.0", "ios")] * 2 + [
            ClientInfo("happ", "5.6.0", "ios")
        ] * 2
        observed = observed_versions(clients, min_devices=2)
        assert observed == {"happ/ios/4": "4.2.0", "happ/ios/5": "5.6.0"}

    def test_routers_are_not_counted(self) -> None:
        clients = [ClientInfo("happ", "9.0.0", ROUTER_PLATFORM)] * 3
        assert observed_versions(clients, min_devices=2) == {}

    def test_a_pinned_channel_reads_its_own_major(self) -> None:
        cache = VersionsCache(observed={"happ/ios/4": "4.2.0", "happ/ios/5": "5.6.0"})
        channel = merge_sources({})["happ"]["channels"]["ios_ru"]
        assert observed_for(cache, "happ", channel) == "4.2.0"
