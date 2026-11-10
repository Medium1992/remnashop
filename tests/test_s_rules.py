"""Rules as text in Telegram: written by the admin where the link goes, shown
to accept, on /rules and from the menu."""

from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity
from aiogram.types import User as AiogramUser

from src.application.common import Notifier
from src.application.common.dao import SettingsDao
from src.application.dto import SettingsDto, UserDto
from src.application.use_cases.access.commands.validation import AcceptRules
from src.application.use_cases.access.queries.requirements import (
    CheckRules,
    CheckRulesResultDto,
)
from src.application.use_cases.s.settings import SetRulesText
from src.core.constants import CONTAINER_KEY, USER_KEY
from src.core.enums import Role
from src.core.exceptions import PermissionDeniedError
from src.telegram.keyboards import CALLBACK_RULES_ACCEPT
from src.telegram.middlewares.rules import RulesMiddleware
from src.telegram.routers.s.rules import is_rules_text, on_rules_text_input
from src.telegram.s_rules import rules_payload

ADMIN = UserDto(id=1, telegram_id=1, name="Admin", role=Role.OWNER)
USER = UserDto(id=2, telegram_id=2, name="User")
RULES = "<b>1.</b> Не раздавать ссылку"


def message(text: str, entities: Optional[list[MessageEntity]] = None) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
        from_user=AiogramUser(id=1, is_bot=False, first_name="A"),
        text=text,
        entities=entities,
    )


class FakeSettingsDao:
    def __init__(self, text: str = "") -> None:
        self.settings = SettingsDto()
        self.settings.extra.s.rules.text = text

    async def get(self) -> SettingsDto:
        return self.settings


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], Any]] = []

    async def notify_user(
        self, user: Any, payload: Any = None, i18n_key: str = "", **kwargs: Any
    ) -> None:
        if payload is not None:
            self.sent.append((payload.i18n_key, payload.i18n_kwargs, payload.reply_markup))
        else:
            self.sent.append((i18n_key, {}, None))


class FakeSetRulesText:
    def __init__(self, settings: FakeSettingsDao, allowed: bool = True) -> None:
        self.settings = settings
        self.allowed = allowed

    async def __call__(self, actor: UserDto, html: str) -> str:
        if not self.allowed:
            raise PermissionDeniedError()
        text = "" if html.strip() == "-" else html.strip()
        self.settings.settings.extra.s.rules.text = text
        return text


class Container:
    def __init__(self, deps: dict[Any, Any]) -> None:
        self.deps = deps

    async def get(self, kind: Any) -> Any:
        return self.deps[kind]


class Manager:
    def __init__(self, container: Container) -> None:
        self.middleware_data = {CONTAINER_KEY: container, USER_KEY: ADMIN}
        self.show_mode: Any = None


class TestWhatCountsAsRules:
    @pytest.mark.parametrize("text", ["https://example.com/tos", "  https://t.me/x  "])
    def test_a_link_stays_upstreams(self, text: str) -> None:
        assert not is_rules_text(message(text))

    @pytest.mark.parametrize("text", ["1. Не раздавать ссылку", "-"])
    def test_any_other_text_is_the_rules(self, text: str) -> None:
        assert is_rules_text(message(text))


class TestAdminInput:
    def setup(self, allowed: bool = True) -> tuple[FakeSettingsDao, FakeNotifier, Manager]:
        settings = FakeSettingsDao()
        notifier = FakeNotifier()
        container = Container(
            {Notifier: notifier, SetRulesText: FakeSetRulesText(settings, allowed)}
        )
        return settings, notifier, Manager(container)

    async def test_saved_with_its_formatting_and_shown_back(self) -> None:
        settings, notifier, manager = self.setup()
        sent = message("1. Не раздавать ссылку", [MessageEntity(type="bold", offset=0, length=2)])
        answers: list[str] = []

        async def answer(text: str, **kwargs: Any) -> None:
            answers.append(text)

        object.__setattr__(sent, "answer", answer)
        await on_rules_text_input(sent, manager)  # type: ignore[arg-type]

        assert settings.settings.extra.s.rules.text == RULES
        assert answers == [RULES]
        assert notifier.sent[-1][0] == "ntf-s-rules.saved"

    async def test_a_dash_goes_back_to_the_link(self) -> None:
        settings, notifier, manager = self.setup()
        settings.settings.extra.s.rules.text = RULES
        await on_rules_text_input(message("-"), manager)  # type: ignore[arg-type]
        assert settings.settings.extra.s.rules.text == ""
        assert notifier.sent[-1][0] == "ntf-s-rules.cleared"

    async def test_what_telegram_refuses_is_not_saved(self) -> None:
        settings, notifier, manager = self.setup()
        sent = message("очень длинные правила")

        async def answer(text: str, **kwargs: Any) -> None:
            raise TelegramBadRequest(SendMessage(chat_id=1, text=text), "message is too long")

        object.__setattr__(sent, "answer", answer)
        await on_rules_text_input(sent, manager)  # type: ignore[arg-type]

        assert settings.settings.extra.s.rules.text == ""
        assert notifier.sent[-1][0] == "ntf-s-rules.refused"

    async def test_only_with_the_permission(self) -> None:
        settings, notifier, manager = self.setup(allowed=False)
        sent = message("правила")

        async def answer(text: str, **kwargs: Any) -> None:
            pass

        object.__setattr__(sent, "answer", answer)
        await on_rules_text_input(sent, manager)  # type: ignore[arg-type]
        assert settings.settings.extra.s.rules.text == ""
        assert notifier.sent[-1][0] == "ntf-s-texts.denied"


class FakeCheckRules:
    async def __call__(self, user: Any) -> CheckRulesResultDto:
        return CheckRulesResultDto(is_required=True, is_accepted=False, rules_url="https://x.y")


class TestAcceptance:
    async def run(self, text: str) -> FakeNotifier:
        notifier = FakeNotifier()
        container = Container(
            {
                CheckRules: FakeCheckRules(),
                AcceptRules: None,
                Notifier: notifier,
                SettingsDao: FakeSettingsDao(text),
            }
        )
        handled: list[bool] = []

        async def handler(event: Any, data: Any) -> None:
            handled.append(True)

        await RulesMiddleware().middleware_logic(
            handler, message("/start"), {CONTAINER_KEY: container, USER_KEY: USER}
        )
        assert handled == []  # nobody gets past without accepting
        return notifier

    async def test_the_rules_text_with_the_accept_button(self) -> None:
        key, kwargs, markup = (await self.run(RULES)).sent[0]
        assert (key, kwargs) == ("ntf-s-rules.accept", {"rules": RULES})
        assert markup.inline_keyboard[0][0].callback_data == CALLBACK_RULES_ACCEPT

    async def test_without_a_text_the_link_as_before(self) -> None:
        key, kwargs, _ = (await self.run("")).sent[0]
        assert (key, kwargs) == ("ntf-requirement.rules-accept-required", {"url": "https://x.y"})


def test_to_read_it_closes_and_stays() -> None:
    payload = rules_payload(RULES, accept=False)
    assert (payload.i18n_key, payload.i18n_kwargs) == ("ntf-s-rules.read", {"rules": RULES})
    assert (payload.disable_default_markup, payload.delete_after) == (False, None)


def test_off_by_default() -> None:
    assert SettingsDto().extra.s.rules.text == ""
