"""Rules as text: what the acceptance notice, /rules and the menu show.

Apart from the router (routers/s/rules.py) so the middleware can use it
without importing every dialog.
"""

from typing import Any, Optional

from src.application.dto import MessagePayloadDto, SettingsDto


def rules_text(settings: SettingsDto) -> str:
    return settings.extra.s.rules.text


def rules_payload(
    text: str, *, accept: bool, reply_markup: Optional[Any] = None
) -> MessagePayloadDto:
    """The rules as a message: with the accept button, or to read and close."""
    if accept:
        return MessagePayloadDto(
            i18n_key="ntf-s-rules.accept",
            i18n_kwargs={"rules": text},
            reply_markup=reply_markup,
            delete_after=None,
        )
    return MessagePayloadDto(
        i18n_key="ntf-s-rules.read",
        i18n_kwargs={"rules": text},
        disable_default_markup=False,
        delete_after=None,
    )
