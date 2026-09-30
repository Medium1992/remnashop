import pytest
from packaging.version import Version

from src.core.panel import (
    PanelLine,
    is_ahead_of_known,
    is_supported,
    uses_expiration_event,
)

# Every tag the panel actually shipped in the supported range, taken from the
# panel repository, so the classification is checked against reality rather than
# against a couple of hand-picked examples.
V2_TAGS = ["2.7.0", "2.7.1", "2.7.2", "2.7.3", "2.7.4", "2.8.0", "2.8.1"]
V3_TAGS = [
    "3.0.0",
    "3.1.0",
    "3.2.0",
    "3.2.1",
    "3.2.2",
    "3.2.3",
    "3.3.0",
    "3.3.1",
    "3.3.2",
    "3.4.0",
    "3.4.1",
    "3.4.2",
]


class TestLineClassification:
    @pytest.mark.parametrize("tag", V2_TAGS)
    def test_two_x_panels_are_the_v2_line(self, tag: str) -> None:
        assert PanelLine.of(Version(tag)) is PanelLine.V2
        assert not PanelLine.of(Version(tag)).is_v3

    @pytest.mark.parametrize("tag", V3_TAGS)
    def test_three_x_panels_are_the_v3_line(self, tag: str) -> None:
        assert PanelLine.of(Version(tag)) is PanelLine.V3
        assert PanelLine.of(Version(tag)).is_v3

    def test_the_boundary_is_exactly_three_zero_zero(self) -> None:
        # 2.8.1 was the last panel with users.uuid; 3.0.0 dropped it.
        assert PanelLine.of(Version("2.8.1")) is PanelLine.V2
        assert PanelLine.of(Version("3.0.0")) is PanelLine.V3

    @pytest.mark.parametrize(
        "reported", ["3.0.0rc1", "3.0.0-rc.1", "3.0.0-beta.2", "3.4.2+build.7"]
    )
    def test_prereleases_of_three_count_as_v3(self, reported: str) -> None:
        # The panel reports whatever is in its package.json. Under PEP 440 a
        # release candidate sorts below the release, which would silently file a
        # 3.x panel under the 2.x contract.
        assert PanelLine.of(Version(reported)) is PanelLine.V3

    def test_prereleases_of_two_eight_stay_on_v2(self) -> None:
        assert PanelLine.of(Version("2.8.0-rc.1")) is PanelLine.V2


class TestSupportWindow:
    @pytest.mark.parametrize("tag", V2_TAGS + V3_TAGS)
    def test_every_shipped_tag_in_range_is_supported(self, tag: str) -> None:
        assert is_supported(Version(tag))

    @pytest.mark.parametrize("tag", ["2.6.3", "2.6.4", "2.0.0", "1.0.0"])
    def test_panels_below_the_floor_are_rejected(self, tag: str) -> None:
        assert not is_supported(Version(tag))

    @pytest.mark.parametrize("tag", V2_TAGS + V3_TAGS)
    def test_no_verified_tag_is_flagged_as_unknown(self, tag: str) -> None:
        assert not is_ahead_of_known(Version(tag))

    @pytest.mark.parametrize("tag", ["3.5.0", "3.6.0", "4.0.0"])
    def test_future_lines_are_flagged_as_unknown(self, tag: str) -> None:
        # Flagged, not rejected: the bot still runs, the operator gets a warning.
        assert is_ahead_of_known(Version(tag))
        assert is_supported(Version(tag))


class TestExpirationEvents:
    @pytest.mark.parametrize("tag", ["2.7.0", "2.7.4"])
    def test_panels_before_two_eight_use_the_legacy_events(self, tag: str) -> None:
        assert not uses_expiration_event(Version(tag))

    @pytest.mark.parametrize("tag", ["2.8.0", "2.8.1"] + V3_TAGS)
    def test_panels_from_two_eight_use_the_single_event(self, tag: str) -> None:
        assert uses_expiration_event(Version(tag))

    def test_the_event_switch_is_independent_of_the_line_boundary(self) -> None:
        # 2.8.x is still the V2 contract but already emits the new event, so the
        # two decisions must not be collapsed into one.
        v28 = Version("2.8.0")
        assert PanelLine.of(v28) is PanelLine.V2
        assert uses_expiration_event(v28)
