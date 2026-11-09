"""``S_SUPPORT_URL``: one link for every "contact support" button."""

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from src.core.utils.s_support import SUPPORT_URL_ENV
from src.infrastructure.services.bot import BotService


def service() -> BotService:
    config: Any = SimpleNamespace(bot=SimpleNamespace(support_username=SecretStr("my_support")))
    return BotService(bot=None, config=config)  # type: ignore[arg-type]


class TestSupportUrl:
    def test_unset_keeps_the_username_link_with_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SUPPORT_URL_ENV, raising=False)

        assert (
            service().get_support_url("Привет")
            == "https://t.me/my_support?text=%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82"
        )

    def test_set_is_used_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SUPPORT_URL_ENV, " https://t.me/my_channel?direct ")

        assert service().get_support_url("Привет") == "https://t.me/my_channel?direct"
        assert service().get_support_url() == "https://t.me/my_channel?direct"

    def test_not_a_url_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SUPPORT_URL_ENV, "t.me/my_channel")

        assert service().get_support_url() == "https://t.me/my_support?text="
