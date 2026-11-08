"""The containers build with the "s" services in them.

dishka checks the whole dependency graph when a container is made, so a
service asking for something no provider gives fails here, not at the first
scheduled run in production.
"""

import pytest

from src.core.config import AppConfig
from src.infrastructure.di.ioc import create_aiogram_container, create_taskiq_container


@pytest.mark.parametrize("factory", [create_aiogram_container, create_taskiq_container])
def test_the_container_builds(factory: object) -> None:
    factory(AppConfig.get(), object())  # type: ignore[operator]
