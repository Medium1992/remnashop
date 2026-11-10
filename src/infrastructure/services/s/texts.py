"""The bot's texts, edited from the admin panel (docs/s/README.md).

An edit is written to the locale's ``custom.ftl`` in the user volume, the same
file an admin may edit by hand, and every process picks it up within seconds:
the storage below re-reads the file when it changes.

Unlike upstream, the overrides are merged into the built-in texts instead of
living in a bundle of their own: an edited text may refer to a built-in one
(``{ frg-user }``), and overriding one attribute keeps the others.
"""

import asyncio
import os
import re
import textwrap
import threading
import time
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Final, Iterator, Optional

from adaptix import Retort
from fluent.syntax import ast, parse, serialize
from fluent.syntax.serializer import serialize_pattern
from fluent.syntax.visitor import Visitor
from fluent_compiler.bundle import FluentBundle
from fluentogram import FluentTranslator
from fluentogram.exceptions import FormatError
from loguru import logger

from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import UserDto
from src.core.config import AppConfig
from src.core.exceptions import PermissionDeniedError
from src.infrastructure.services.i18n import LayeredFileStorage
from src.infrastructure.services.translator import TranslatorHubImpl, TranslatorRunnerImpl

CUSTOM_FILE: Final[str] = "custom.ftl"
# How often a process looks at custom.ftl for changes.
RELOAD_SECONDS: Final[float] = 2.0
SEARCH_LIMIT: Final[int] = 40

_PROBE: Final[str] = "s-probe"
_INDENT: Final[str] = "    "


class TextError(ValueError):
    """An edit that can not be saved; ``reason`` names the message for the admin."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ── Fluent plumbing ──────────────────────────────────────────────────────────


def pattern_text(pattern: ast.Pattern) -> str:
    """A pattern as the admin sees and types it: the file's text, unindented."""
    raw = serialize_pattern(pattern)
    if raw.startswith("\n"):
        return textwrap.dedent(raw[1:])
    return raw.lstrip(" ")


def parse_text(text: str) -> ast.Pattern:
    """The admin's text as a pattern; ``TextError("syntax")`` if Fluent can't read it."""
    text = text.strip("\n")
    if not text.strip():
        raise TextError("empty")
    body = textwrap.indent(text, _INDENT, lambda line: True)
    resource = parse(f"{_PROBE} =\n{body}\n")
    entries = resource.body
    if len(entries) != 1 or not isinstance(entries[0], ast.Message):
        junk = next((e for e in entries if isinstance(e, ast.Junk)), None)
        detail = (junk.annotations[0].message or "") if junk and junk.annotations else ""
        raise TextError("syntax", detail)
    message = entries[0]
    if message.value is None or message.attributes:
        # A line starting with "." reads as an attribute of the probe.
        raise TextError("syntax", "a line can not start with '.'")
    return message.value


def _keys(message: ast.Message) -> Iterator[tuple[str, ast.Pattern]]:
    if message.value is not None:
        yield message.id.name, message.value
    for attribute in message.attributes:
        yield f"{message.id.name}.{attribute.id.name}", attribute.value


class _References(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.variables: set[str] = set()
        self.messages: set[str] = set()

    def visit_VariableReference(self, node: ast.VariableReference) -> None:  # noqa: N802
        self.variables.add(node.id.name)

    def visit_MessageReference(self, node: ast.MessageReference) -> None:  # noqa: N802
        name = node.id.name
        self.messages.add(f"{name}.{node.attribute.name}" if node.attribute else name)


def _references(pattern: ast.Pattern) -> _References:
    found = _References()
    found.visit(pattern)
    return found


def _read(path: Path) -> ast.Resource:
    resource = parse(path.read_text("utf8"))
    for junk in (e for e in resource.body if isinstance(e, ast.Junk)):
        logger.warning(f"Unreadable text in '{path}': {(junk.content or '').strip()[:80]!r}")
    return resource


def read_overrides(path: Path) -> dict[str, ast.Pattern]:
    if not path.exists():
        return {}
    found: dict[str, ast.Pattern] = {}
    for entry in _read(path).body:
        if isinstance(entry, ast.Message):
            found.update(_keys(entry))
    return found


def merge(defaults: list[ast.Resource], overrides: dict[str, ast.Pattern]) -> ast.Resource:
    """The built-in texts with ``overrides`` put in, key by key."""
    left = dict(overrides)
    body: list[ast.EntryType] = []
    for resource in defaults:
        for entry in resource.body:
            if not isinstance(entry, (ast.Message, ast.Term)):
                continue
            if isinstance(entry, ast.Message):
                entry = entry.clone()
                name = entry.id.name
                if name in left:
                    entry.value = left.pop(name)
                for attribute in entry.attributes:
                    key = f"{name}.{attribute.id.name}"
                    if key in left:
                        attribute.value = left.pop(key)
            body.append(entry)

    # Keys of the admin's own (custom-*), not among the built-in ones.
    extra: dict[str, ast.Message] = {}
    for key, pattern in left.items():
        name, _, attr = key.partition(".")
        message = extra.setdefault(name, ast.Message(ast.Identifier(name)))
        if attr:
            message.attributes.append(ast.Attribute(ast.Identifier(attr), pattern))
        else:
            message.value = pattern
    body.extend(extra.values())
    return ast.Resource(body)


def _closure(resource: ast.Resource, name: str) -> ast.Resource:
    """Message ``name``, the messages it uses, and every term: all it takes to
    render it, compiled in a blink instead of the whole set's second or two."""
    messages = {e.id.name: e for e in resource.body if isinstance(e, ast.Message)}
    terms = [e for e in resource.body if isinstance(e, ast.Term)]
    needed: dict[str, ast.Message] = {}
    todo = [name]
    while todo:
        current = todo.pop()
        if current in needed or current not in messages:
            continue
        message = needed[current] = messages[current]
        for _, pattern in _keys(message):
            todo.extend(ref.partition(".")[0] for ref in _references(pattern).messages)
    return ast.Resource([*terms, *needed.values()])


def compile_translator(locale: str, resource: ast.Resource) -> FluentTranslator:
    return _compiled(locale, serialize(resource))


@lru_cache(maxsize=8)
def _compiled(locale: str, text: str) -> FluentTranslator:
    # The same texts compile to the same functions: no second or two twice.
    return FluentTranslator(
        locale=locale,
        translator=FluentBundle.from_string(  # type: ignore[no-untyped-call]
            locale=locale, text=text, use_isolating=False
        ),
    )


def write_override(path: Path, key: str, pattern: Optional[ast.Pattern]) -> None:
    """Put ``key`` into custom.ftl, or take it out for ``None``; comments stay."""
    resource = parse(path.read_text("utf8")) if path.exists() else ast.Resource([])
    name, _, attr = key.partition(".")
    message = next(
        (e for e in resource.body if isinstance(e, ast.Message) and e.id.name == name), None
    )

    if pattern is not None:
        if message is None:
            message = ast.Message(ast.Identifier(name))
            resource.body.append(message)
        if not attr:
            message.value = pattern
        else:
            current = next((a for a in message.attributes if a.id.name == attr), None)
            if current is None:
                message.attributes.append(ast.Attribute(ast.Identifier(attr), pattern))
            else:
                current.value = pattern
    elif message is not None:
        if attr:
            message.attributes = [a for a in message.attributes if a.id.name != attr]
        else:
            message.value = None
        if message.value is None and not message.attributes:
            resource.body.remove(message)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(serialize(resource, with_junk=True), "utf8")
    os.replace(temporary, path)


# ── the storage every process reads texts from ───────────────────────────────


class SLayeredFileStorage(LayeredFileStorage):
    """Upstream's storage, with custom.ftl merged in and re-read on change.

    Compiling the texts takes a second or two, so a change is compiled in a
    thread while the texts from before it keep being served.
    """

    def __init__(self, user_translations_dir: Path, default_translations_dir: Path) -> None:
        self._stamps: dict[str, tuple[int, int]] = {}
        self._defaults: dict[str, list[ast.Resource]] = {}
        self._building: set[str] = set()
        self._lock = threading.Lock()
        self._checked = 0.0
        super().__init__(user_translations_dir, default_translations_dir)

    @property
    def defaults_dir(self) -> Path:
        # Local runs have no assets.default: every file sits in assets.
        return self._default_dir if self._default_dir.exists() else self._user_dir

    def custom_path(self, locale: str) -> Path:
        return self._user_dir / locale / CUSTOM_FILE

    def default_resources(self, locale: str) -> list[ast.Resource]:
        # The built-in texts ship in the image and never change while it runs.
        if locale not in self._defaults:
            files = sorted((self.defaults_dir / locale).rglob("*.ftl"))
            self._defaults[locale] = [_read(f) for f in files if f.name != CUSTOM_FILE]
        return self._defaults[locale]

    def _stamp(self, locale: str) -> tuple[int, int]:
        try:
            stat = self.custom_path(locale).stat()
        except FileNotFoundError:
            return (0, 0)
        return (stat.st_mtime_ns, stat.st_size)

    def rebuild(self, locale: str) -> None:
        # The stamp is taken first: a write during the build is seen next time.
        stamp = self._stamp(locale)
        merged = merge(self.default_resources(locale), read_overrides(self.custom_path(locale)))
        translator = compile_translator(locale, merged)
        self._default_translators[locale] = translator
        self.add_translator(translator)
        self._stamps[locale] = stamp

    def _rebuild_in_background(self, locale: str) -> None:
        try:
            self.rebuild(locale)
            logger.info(f"Texts of '{locale}' reloaded")
        except Exception as exception:
            # A broken hand edit: keep serving the texts from before it.
            self._stamps[locale] = self._stamp(locale)
            logger.error(f"Texts of '{locale}' were not reloaded: {exception}")
        finally:
            with self._lock:
                self._building.discard(locale)

    def _load_translations(self) -> None:
        for locale_dir in sorted(self.defaults_dir.iterdir()):
            if locale_dir.is_dir():
                self.rebuild(locale_dir.name)
        self._checked = time.monotonic()

    def reload(self) -> None:
        if time.monotonic() - self._checked < RELOAD_SECONDS:
            return
        self._checked = time.monotonic()
        for locale in list(self._default_translators):
            if self._stamp(locale) == self._stamps.get(locale):
                continue
            with self._lock:
                if locale in self._building:
                    continue
                self._building.add(locale)
            threading.Thread(
                target=self._rebuild_in_background, args=(locale,), daemon=True
            ).start()

    def get_translators_for_language(self, language: str) -> list[FluentTranslator]:
        self.reload()
        return super().get_translators_for_language(language)


# ── what the admin panel works with ──────────────────────────────────────────


@dataclass(frozen=True)
class TextEntry:
    key: str
    default: ast.Pattern
    override: Optional[ast.Pattern] = None
    # Placeholders the text is given, collected from it and the texts it uses.
    variables: frozenset[str] = field(default_factory=frozenset)

    @property
    def changed(self) -> bool:
        return self.override is not None

    @cached_property
    def text(self) -> str:
        return pattern_text(self.override or self.default)

    @cached_property
    def default_text(self) -> str:
        return pattern_text(self.default)

    def snippet(self, width: int = 40) -> str:
        plain = re.sub(r"<[^>]+>|\{[^{}]*\}", " ", self.text)
        plain = " ".join(plain.split()) or self.key
        return plain if len(plain) <= width else plain[: width - 1] + "…"


class TextOverrides:
    """Search, check and change the bot's texts in the default locale."""

    def __init__(self, config: AppConfig, hub: TranslatorHubImpl) -> None:
        if not isinstance(hub.storage, SLayeredFileStorage):
            raise TypeError("Texts can be edited with SLayeredFileStorage only")
        self.storage: SLayeredFileStorage = hub.storage
        self.locale = str(config.default_locale)
        self._patterns: Optional[dict[str, ast.Pattern]] = None

    @property
    def path(self) -> Path:
        return self.storage.custom_path(self.locale)

    def _default_resources(self) -> list[ast.Resource]:
        return self.storage.default_resources(self.locale)

    def _default_patterns(self) -> dict[str, ast.Pattern]:
        if self._patterns is None:
            self._patterns = {}
            for resource in self._default_resources():
                for entry in resource.body:
                    if isinstance(entry, ast.Message):
                        self._patterns.update(_keys(entry))
        return self._patterns

    @staticmethod
    def _needs(pattern: ast.Pattern, defaults: dict[str, ast.Pattern]) -> set[str]:
        """Placeholders ``pattern`` renders, its own and those of the texts it uses."""
        found: set[str] = set()
        seen: set[str] = set()
        todo = [pattern]
        while todo:
            refs = _references(todo.pop())
            found |= refs.variables
            for key in refs.messages - seen:
                seen.add(key)
                if key in defaults:
                    todo.append(defaults[key])
        return found

    def entries(self) -> list[TextEntry]:
        defaults = self._default_patterns()
        overrides = read_overrides(self.path)
        return [
            TextEntry(
                key=key,
                default=pattern,
                override=overrides.get(key),
                variables=frozenset(self._needs(pattern, defaults)),
            )
            for key, pattern in defaults.items()
        ]

    def entry(self, key: str) -> TextEntry:
        found = next((e for e in self.entries() if e.key == key), None)
        if found is None:
            raise TextError("unknown", key)
        return found

    def search(self, query: str) -> list[TextEntry]:
        needle = " ".join(query.split()).casefold()
        if not needle:
            return []
        found = [
            e
            for e in self.entries()
            if needle in e.key.casefold()
            or needle in " ".join(e.text.split()).casefold()
            or needle in " ".join(e.default_text.split()).casefold()
        ]
        return found[:SEARCH_LIMIT]

    def changed(self) -> list[TextEntry]:
        return [e for e in self.entries() if e.changed]

    async def check(self, key: str, text: str) -> str:
        """The new text rendered with sample values; ``TextError`` if it won't work."""
        # Compiling every text takes a second or two: off the event loop.
        return await asyncio.to_thread(self._check, key, text)

    def _check(self, key: str, text: str) -> str:
        entry = self.entry(key)
        pattern = parse_text(text)

        unknown = self._needs(pattern, self._default_patterns()) - entry.variables
        if unknown:
            raise TextError("variable", ", ".join(f"${v}" for v in sorted(unknown)))

        overrides = read_overrides(self.path)
        overrides[key] = pattern
        merged = merge(self._default_resources(), overrides)
        translator = compile_translator(self.locale, _closure(merged, key.partition(".")[0]))
        runner = TranslatorRunnerImpl(translators=[translator], retort=Retort())
        # A name in angle quotes for each placeholder; a number where one is due.
        sample: dict[str, object] = {name: f"‹{name}›" for name in entry.variables}
        for _ in range(len(sample) + 1):
            try:
                return runner.get(key, **sample)
            except FormatError as error:
                cause = str(error.args[0] if error.args else error)
                # "<string>:2800:25: Unknown message: x" -> "Unknown message: x"
                raise TextError("render", re.sub(r"^<string>:\d+:\d+: ", "", cause))
            except TypeError as error:
                numeric = re.search(r"object ‹([\w-]+)›", str(error))
                if not numeric:
                    raise TextError("render", str(error))
                sample[numeric[1]] = 1
        raise TextError("render")

    @staticmethod
    def ensure_allowed(actor: UserDto) -> None:
        if not PermissionPolicy.has_permission(actor, Permission.SETTINGS_EXTRA):
            raise PermissionDeniedError()

    async def save(self, actor: UserDto, key: str, text: str) -> None:
        self.ensure_allowed(actor)
        await self.check(key, text)
        write_override(self.path, key, parse_text(text))
        await asyncio.to_thread(self.storage.rebuild, self.locale)
        logger.info(f"{actor.log} Changed text '{key}'")

    async def reset(self, actor: UserDto, key: str) -> None:
        self.ensure_allowed(actor)
        self.entry(key)
        write_override(self.path, key, None)
        await asyncio.to_thread(self.storage.rebuild, self.locale)
        logger.info(f"{actor.log} Reset text '{key}' to the built-in one")
