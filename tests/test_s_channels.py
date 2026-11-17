"""Where each payment gateway is offered: the bot shows Stars only by default."""

from types import SimpleNamespace
from typing import Any

import pytest
from adaptix import Retort
from dishka import make_container

from src.application.dto import ExtraSettingsDto, SettingsDto
from src.application.dto.s_settings import GatewayChannel, GatewayChannelsSettingsDto
from src.core.enums import PaymentGatewayType
from src.infrastructure.database.dao.base import BaseDaoImpl
from src.infrastructure.database.models import Settings
from src.infrastructure.di.providers.retort import RetortProvider
from src.infrastructure.services.s.channels import GatewayChannels

BOT, WEB = GatewayChannel.BOT, GatewayChannel.WEB
STARS, YOOKASSA, CRYPTOPAY = "TELEGRAM_STARS", "YOOKASSA", "CRYPTOPAY"


class TestDefault:
    def test_the_bot_offers_stars_alone(self) -> None:
        channels = GatewayChannelsSettingsDto()
        assert channels.offers(STARS, BOT)
        assert not channels.offers(YOOKASSA, BOT)
        assert not channels.offers(CRYPTOPAY, BOT)

    def test_the_cabinet_offers_everything_but_stars(self) -> None:
        channels = GatewayChannelsSettingsDto()
        assert not channels.offers(STARS, WEB)
        assert channels.offers(YOOKASSA, WEB) and channels.offers(CRYPTOPAY, WEB)

    def test_every_gateway_type_is_somewhere(self) -> None:
        channels = GatewayChannelsSettingsDto()
        for kind in PaymentGatewayType:
            assert channels.offers(kind.value, BOT) != channels.offers(kind.value, WEB)


class TestToggle:
    def test_turning_one_on_spells_out_the_default(self) -> None:
        channels = GatewayChannelsSettingsDto()
        assert channels.toggle(YOOKASSA, BOT) is True
        assert channels.bot == [STARS, YOOKASSA]
        assert channels.web is None  # the other channel keeps its default

    def test_turning_off_and_on_again(self) -> None:
        channels = GatewayChannelsSettingsDto()
        assert channels.toggle(STARS, BOT) is False
        assert channels.bot == [] and not channels.offers(STARS, BOT)
        assert channels.toggle(STARS, BOT) is True
        assert channels.offers(STARS, BOT)

    def test_the_cabinet_list(self) -> None:
        channels = GatewayChannelsSettingsDto()
        assert channels.toggle(CRYPTOPAY, WEB) is False
        assert not channels.offers(CRYPTOPAY, WEB) and channels.offers(YOOKASSA, WEB)
        assert STARS not in (channels.web or [])


@pytest.fixture
def retort() -> Retort:
    return make_container(RetortProvider(), skip_validation=True).get(Retort)


class TestStored:
    def test_an_old_row_loads_with_the_default(self, retort: Retort) -> None:
        extra = retort.load({"s": {"slots": {"enabled": True}}}, ExtraSettingsDto)
        assert extra.s.channels.bot is None and extra.s.channels.web is None
        assert not extra.s.channels.notified

    def test_a_chosen_list_is_written_back(self, retort: Retort) -> None:
        settings = retort.load(retort.dump(SettingsDto()), SettingsDto)
        settings.extra.s.channels.toggle(YOOKASSA, BOT)
        values = BaseDaoImpl(session=None, retort=retort)._serialize_for_update(  # type: ignore[arg-type]
            settings, SettingsDto, Settings
        )
        merged = values["extra"].right.value
        assert merged["s"]["channels"]["bot"] == [STARS, YOOKASSA]
        reloaded = retort.load({"s": merged["s"]}, ExtraSettingsDto)
        assert reloaded.s.channels.offers(YOOKASSA, BOT)


# -- the service ---------------------------------------------------------------


def gateway(kind: str, active: bool = True) -> Any:
    return SimpleNamespace(type=PaymentGatewayType(kind), is_active=active)


class Uow:
    async def __aenter__(self) -> "Uow":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class Gateways:
    def __init__(self, *gateways: Any) -> None:
        self.gateways = list(gateways)

    async def get_active(self) -> list[Any]:
        return [g for g in self.gateways if g.is_active]


class SettingsStore:
    def __init__(self, channels: GatewayChannelsSettingsDto) -> None:
        self.settings = SimpleNamespace(extra=SimpleNamespace(s=SimpleNamespace(channels=channels)))
        self.updates = 0

    async def get(self) -> Any:
        return self.settings

    async def update(self, settings: Any) -> None:
        self.updates += 1


class Notifier:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def notify_admins(self, payload: Any, roles: Any = None) -> None:
        self.sent.append(payload)


def service(
    channels: GatewayChannelsSettingsDto, *gateways: Any
) -> tuple[GatewayChannels, SettingsStore, Notifier]:
    store, notifier = SettingsStore(channels), Notifier()
    return (
        GatewayChannels(Uow(), Gateways(*gateways), store, notifier),  # type: ignore[arg-type]
        store,
        notifier,
    )


async def test_the_bot_gets_its_own_gateways_in_order() -> None:
    channels, _, _ = service(
        GatewayChannelsSettingsDto(),
        gateway(YOOKASSA),
        gateway(STARS),
        gateway(CRYPTOPAY, active=False),
    )
    assert [g.type for g in await channels.for_bot()] == [STARS]
    assert [g.type for g in await channels.active(WEB)] == [YOOKASSA]


async def test_an_old_button_for_a_gateway_out_of_the_bot_is_refused() -> None:
    channels, _, _ = service(GatewayChannelsSettingsDto(), gateway(YOOKASSA), gateway(STARS))
    assert not await channels.allows(PaymentGatewayType.YOOKASSA, BOT)
    assert await channels.allows(PaymentGatewayType.TELEGRAM_STARS, BOT)


async def test_the_owner_is_told_once_what_left_the_bot() -> None:
    channels, store, notifier = service(
        GatewayChannelsSettingsDto(), gateway(STARS), gateway(YOOKASSA), gateway(CRYPTOPAY)
    )
    await channels.tell_once()
    await channels.tell_once()
    (payload,) = notifier.sent
    assert payload.i18n_key == "event-s-channels.bot-stars-only"
    assert payload.i18n_kwargs == {"gateways": f"{YOOKASSA}, {CRYPTOPAY}"}
    assert store.settings.extra.s.channels.notified


async def test_nothing_is_said_when_the_bot_loses_nothing() -> None:
    channels, store, notifier = service(GatewayChannelsSettingsDto(), gateway(STARS))
    await channels.tell_once()
    assert not notifier.sent and store.settings.extra.s.channels.notified
