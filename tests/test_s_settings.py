"""Settings of the "s" edition, stored under ``settings.extra.s``.

They add no column and no migration, so two things must hold: a row written
before they existed loads with every feature off, and a change to one nested
field reaches the UPDATE instead of being lost in the partial JSONB merge.
"""

import pytest
from adaptix import Retort
from dishka import make_container
from packaging.version import Version

from src.application.dto import ExtraSettingsDto, SettingsDto
from src.infrastructure.database.dao.base import BaseDaoImpl
from src.infrastructure.database.models import Settings
from src.infrastructure.di.providers.retort import RetortProvider
from src.infrastructure.taskiq.tasks.update import parse_release_version


@pytest.fixture
def retort() -> Retort:
    return make_container(RetortProvider(), skip_validation=True).get(Retort)


class TestAnOlderRow:
    def test_extra_without_s_loads_with_everything_off(self, retort: Retort) -> None:
        extra = retort.load({"trial_channel_guard": True}, ExtraSettingsDto)

        assert extra.trial_channel_guard is True
        assert extra.s.slots.enabled is False
        assert extra.s.versions.enabled is False
        assert extra.s.stale.enabled is False
        assert extra.s.inactive.enabled is False
        assert extra.s.chat_access.enabled is False

    def test_a_partial_s_block_keeps_the_missing_defaults(self, retort: Retort) -> None:
        extra = retort.load({"s": {"slots": {"enabled": True}}}, ExtraSettingsDto)

        assert extra.s.slots.enabled is True
        assert extra.s.slots.slot_ttl_minutes == 5
        assert extra.s.inactive.delete_after_days == 30


class TestSaving:
    def test_a_nested_change_reaches_the_update(self, retort: Retort) -> None:
        settings = retort.load(retort.dump(SettingsDto()), SettingsDto)
        settings.extra.s.slots.enabled = True

        values = BaseDaoImpl(session=None, retort=retort)._serialize_for_update(  # type: ignore[arg-type]
            settings, SettingsDto, Settings
        )

        assert set(values) == {"extra"}
        # The JSONB merge receives the whole "s" block, so sibling fields are
        # written back with their current values rather than dropped.
        merged = values["extra"].right.value
        assert merged["s"]["slots"] == {"enabled": True, "base_limit": 1, "slot_ttl_minutes": 5}
        assert merged["s"]["inactive"]["enabled"] is False


class TestBuildTag:
    def test_the_s_build_tag_is_a_version_just_above_its_base(self) -> None:
        version = parse_release_version("v0.8.2+3x.s")

        assert version is not None
        assert Version("0.8.2") < version
        assert version < Version("0.8.3")
