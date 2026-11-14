"""Every text the "s" code asks for exists and renders.

A missing key does not fail at runtime -- the translator falls back to the key
itself and the user sees "btn-s.slots-ttl" on a button. So the keys are
collected from the code and each one is rendered here.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from dishka import make_container

from src.application.common import TranslatorHub
from src.core.config import AppConfig
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.di.providers.retort import RetortProvider
from src.telegram.routers.s.dashboard.handlers import NUMBERS, TOGGLES

ROOT = Path(__file__).resolve().parent.parent
S_SOURCES = [
    *ROOT.glob("src/telegram/routers/s/**/*.py"),
    *ROOT.glob("src/telegram/routers/dashboard/users/user/dialog.py"),
    *ROOT.glob("src/infrastructure/services/s/*.py"),
]
KEY_RE = re.compile(r"\"((?:btn|msg|ntf)-s[-\w]*\.[-\w]+)\"")

# Plenty for any placeholder a message uses.
NUMERIC = """
    enabled value seconds occupied limit sideload abandoned users devices sent failed
    unavailable target telegram_id slots_enabled slots_base slots_ttl versions_enabled
    versions_notify versions_ignore_patch versions_interval versions_refresh versions_checked
    stale_enabled stale_hours stale_interval routers_count ch_enabled ch_checked field_low
    field_high inactive_days inactive_warn inactive_enabled blocked blocked_count
    inactive_include_disabled invites_enabled invites_ttl invites_max max_active hours n
    count texts_changed texts_count text_changed s_rules_text s_rules
    s_invite_active s_invite_max plans_calendar
    checked must free amount s_left_days s_left_value s_full_amount s_limit s_count s_must
    s_traffic s_traffic_left s_traffic_limit s_keep connect_page connectable
    bonus_enabled bonus_plan_set s_bonus_days s_bonus_plan_set days total chosen active
"""
TEXTUAL = """
    title app installed latest url label apps devices content name kind field ch_title
    ch_type ch_where ch_url ch_value ch_published ch_status ch_source routers date since
    online status reason until s_invite_links url texts_mode texts_query text_key
    text_source text_variables detail rules subscription_url bonus_plan s_bonus_plan
    currency period s_currency s_keep_date_local s_today_period s_today_date_local s_plan
"""
ARGS: dict[str, Any] = {**dict.fromkeys(NUMERIC.split(), 1), **dict.fromkeys(TEXTUAL.split(), "x")}


def keys_in_code() -> set[str]:
    found = set()
    for path in S_SOURCES:
        found.update(KEY_RE.findall(path.read_text("utf8")))
    for widget_id in [*TOGGLES, *NUMBERS]:
        found.add(f"btn-s.{widget_id.replace('_', '-')}")
    return found


@pytest.fixture(scope="module")
def i18n() -> Any:
    container = make_container(
        RetortProvider(), I18nProvider(), context={AppConfig: AppConfig.get()}, skip_validation=True
    )
    return container.get(TranslatorHub).get_translator_by_locale("ru")


def test_keys_were_found() -> None:
    # Guards the regex: an empty set would make the test below pass vacuously.
    assert len(keys_in_code()) > 20


@pytest.mark.parametrize("key", sorted(keys_in_code()))
def test_the_key_exists_and_renders(i18n: Any, key: str) -> None:
    text = i18n.get(key, **ARGS)
    assert text and text != key
