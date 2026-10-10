"""The build tag, as the hourly update check reads it.

A fork build was first labelled ``3x``. The update check parsed that as a
version, failed, and raised an error alert once an hour. A build tag that is not
a release number has to be a quiet no-op, and the tag the fork now uses has to
compare sensibly against upstream releases.
"""

import pytest
from packaging.version import Version

from src.core.config.build import BuildConfig
from src.core.constants import REPOSITORY
from src.infrastructure.taskiq.tasks.update import parse_release_version


class TestParsingABuildTag:
    @pytest.mark.parametrize(
        "tag, expected",
        [
            ("v0.8.2", "0.8.2"),
            ("0.8.2", "0.8.2"),
            ("v1.0.0", "1.0.0"),
            (" v0.8.2 ", "0.8.2"),
        ],
    )
    def test_a_release_tag_parses(self, tag: str, expected: str) -> None:
        assert parse_release_version(tag) == Version(expected)

    @pytest.mark.parametrize("tag", ["3x", "feat/remnawave-3x", "latest", "main", "x.y.z"])
    def test_a_tag_that_is_not_a_version_is_none_not_an_error(self, tag: str) -> None:
        # This is the case that raised InvalidVersion every hour.
        assert parse_release_version(tag) is None

    @pytest.mark.parametrize("tag", [None, "", "   "])
    def test_a_missing_tag_is_none(self, tag: object) -> None:
        assert parse_release_version(tag) is None  # type: ignore[arg-type]

    def test_only_a_leading_v_is_dropped(self) -> None:
        # Stripping every "v" would mangle a pre-release such as 1.0.0.dev1.
        assert parse_release_version("v1.0.0.dev1") == Version("1.0.0.dev1")


class TestTheForkTag:
    """The fork labels its builds ``<upstream release>+3x``."""

    def test_it_is_a_valid_version(self) -> None:
        assert parse_release_version("v0.8.2+3x") is not None

    def test_it_sorts_above_the_release_it_is_built_on(self) -> None:
        # So the bot does not announce the release it already contains.
        fork = parse_release_version("v0.8.2+3x")
        base = parse_release_version("v0.8.2")

        assert fork is not None and base is not None
        assert base <= fork

    @pytest.mark.parametrize("upstream", ["v0.8.3", "v0.9.0", "v1.0.0"])
    def test_the_next_upstream_release_registers_as_newer(self, upstream: str) -> None:
        # That notification is how an operator learns an official release with
        # panel 3.x support has come out.
        fork = parse_release_version("v0.8.2+3x")
        newer = parse_release_version(upstream)

        assert fork is not None and newer is not None
        assert newer > fork


class TestCommitLink:
    def test_a_fork_build_links_into_its_own_repository(self) -> None:
        # The commit does not exist upstream; a link there is a 404.
        build = BuildConfig(commit="ac614d2", repository="https://github.com/someone/remnashop")

        assert build.commit_url == "https://github.com/someone/remnashop/commit/ac614d2"

    def test_an_upstream_build_links_upstream(self) -> None:
        build = BuildConfig(commit="ac614d2")

        assert build.commit_url == f"{REPOSITORY}/commit/ac614d2"

    def test_an_empty_repository_falls_back_to_upstream(self) -> None:
        # The Dockerfile passes the build arg through even when it is not set.
        build = BuildConfig(commit="ac614d2", repository="")

        assert build.commit_url == f"{REPOSITORY}/commit/ac614d2"

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        build = BuildConfig(commit="abc", repository="https://github.com/someone/remnashop/")

        assert build.commit_url == "https://github.com/someone/remnashop/commit/abc"
