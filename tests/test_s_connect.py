"""The "Connect" window: the link to copy, its QR code and the button to open it."""

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from dishka.integrations.aiogram import CONTAINER_NAME
from PIL import Image

import src.infrastructure.services  # noqa: F401  # before the routers, as in the app
from src.application.common.dao import SettingsDao, SubscriptionDao
from src.application.dto import SettingsDto
from src.core.constants import CONFIG_KEY, USER_KEY
from src.telegram import keyboards
from src.telegram.routers.s import connect
from src.telegram.routers.s.connect import connect_getter, qr_png
from src.telegram.s_states import SConnect

URL = "https://sub.example.com/abc"


@pytest.fixture(autouse=True)
def qr_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(connect, "QR_DIR", tmp_path)
    return tmp_path


def test_the_qr_code_is_an_image_made_once_per_link(qr_dir: Any) -> None:
    path = qr_png(URL)
    with Image.open(path) as image:
        assert image.format == "PNG" and image.size[0] > 100
    made = path.stat().st_mtime_ns
    assert qr_png(URL) == path and path.stat().st_mtime_ns == made
    assert qr_png(URL + "x") != path
    assert [p.suffix for p in qr_dir.iterdir()] == [".png", ".png"]


def shown(data: dict[str, Any]) -> list[str]:
    """Ids of the connect buttons shown for the getter's ``data``."""
    ids = []
    for widget in keyboards.connect_buttons:
        if not widget.is_(data, None):  # type: ignore[arg-type]
            continue
        children = getattr(widget, "buttons", [widget])
        ids += [c.widget_id for c in children if c.is_(data, None)]
    return ids


PAGE = {"connectable": True, "is_mini_app": False, "is_mini_app_reserve": False}


def test_off_connect_opens_the_link_as_upstream() -> None:
    assert shown({**PAGE, "s_connect": 0}) == ["connect_sub_page"]
    # A getter that knows nothing of the window keeps upstream's button.
    assert shown(PAGE) == ["connect_sub_page"]


def test_on_connect_opens_the_window() -> None:
    assert shown({**PAGE, "s_connect": 1}) == ["s_connect"]
    start = keyboards.connect_buttons[0]
    assert start.state == SConnect.MAIN


def test_nothing_to_connect_without_an_active_subscription() -> None:
    assert shown({**PAGE, "connectable": False, "s_connect": 1}) == []


class Subscriptions:
    def __init__(self, subscription: Any) -> None:
        self.subscription = subscription

    async def get_current(self, user_id: int) -> Any:
        return self.subscription


class Settings:
    async def get(self) -> SettingsDto:
        return SettingsDto()


def config(mini_app: Optional[str] = None) -> Any:
    bot = SimpleNamespace(mini_app_url=mini_app, is_mini_app=bool(mini_app))
    return SimpleNamespace(bot=bot)


class Container:
    def __init__(self, subscription: Any) -> None:
        self.deps = {SubscriptionDao: Subscriptions(subscription), SettingsDao: Settings()}

    async def get(self, kind: Any) -> Any:
        return self.deps[kind]


async def get(subscription: Any, mini_app: Optional[str] = None) -> dict[str, Any]:
    manager = SimpleNamespace(
        middleware_data={
            CONTAINER_NAME: Container(subscription),
            CONFIG_KEY: config(mini_app),
            USER_KEY: SimpleNamespace(id=7, log="[user]"),
        }
    )
    return await connect_getter(manager)


async def test_the_link_its_code_and_the_page() -> None:
    data = await get(SimpleNamespace(is_active=True, url=URL))
    assert data["connectable"] == 1
    assert (data["subscription_url"], data["connection_url"]) == (URL, URL)
    assert data["s_qr"].path == str(qr_png(URL))


async def test_the_code_is_of_the_link_even_with_a_mini_app() -> None:
    data = await get(SimpleNamespace(is_active=True, url=URL), mini_app="https://app")
    assert (data["subscription_url"], data["connection_url"]) == (URL, "https://app")
    assert data["s_qr"].path == str(qr_png(URL))


@pytest.mark.parametrize(
    "subscription",
    [None, SimpleNamespace(is_active=False, url=URL), SimpleNamespace(is_active=True, url="")],
)
async def test_nothing_to_show_without_an_active_link(subscription: Any) -> None:
    assert await get(subscription) == {"connectable": 0, "s_qr": None}


async def test_the_link_stays_when_the_code_cannot_be_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(url: str) -> Any:
        raise OSError("read-only")

    monkeypatch.setattr(connect, "qr_png", broken)
    data = await get(SimpleNamespace(is_active=True, url=URL))
    assert data["connectable"] == 1 and data["s_qr"] is None
