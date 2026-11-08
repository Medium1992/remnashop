"""The containers build with the "s" services in them.

dishka checks the whole dependency graph when a container is made, so a
service asking for something no provider gives fails here, not at the first
scheduled run in production.
"""

import pytest

from src.application.common import Remnawave
from src.core.config import AppConfig
from src.infrastructure.di.ioc import create_aiogram_container, create_taskiq_container
from src.infrastructure.services.s.remnawave import SRemnawave


@pytest.mark.parametrize("factory", [create_aiogram_container, create_taskiq_container])
def test_the_container_builds(factory: object) -> None:
    factory(AppConfig.get(), object())  # type: ignore[operator]


def test_the_panel_client_is_the_s_one() -> None:
    container = create_taskiq_container(AppConfig.get(), object())  # type: ignore[arg-type]
    sources = [
        factory.source
        for key, factory in container.registry.factories.items()
        if key.type_hint is Remnawave
    ]
    assert sources == [SRemnawave]
