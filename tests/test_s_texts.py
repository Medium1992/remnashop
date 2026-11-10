"""Texts edited from the admin panel: found, checked, saved to custom.ftl,
served by every process without a restart, and reset."""

import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from src.application.dto import UserDto
from src.core.enums import Role
from src.core.exceptions import PermissionDeniedError
from src.infrastructure.services.i18n import LayeredFileStorage
from src.infrastructure.services.s import texts as texts_module
from src.infrastructure.services.s.texts import (
    SLayeredFileStorage,
    TextError,
    TextOverrides,
    parse_text,
    pattern_text,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "assets/translations/ru/custom.ftl").read_text("utf8")
MAINTENANCE = "ntf-access.maintenance"

ADMIN = UserDto(id=1, telegram_id=1, name="Admin", role=Role.OWNER)
NOBODY = UserDto(id=2, telegram_id=2, name="User", role=Role.USER)


class _Hub:
    def __init__(self, storage: SLayeredFileStorage) -> None:
        self.storage = storage


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """The image's built-in texts and the user volume with its custom.ftl."""
    defaults = tmp_path / "assets.default"
    shutil.copytree(ROOT / "assets/translations", defaults)
    user = tmp_path / "assets" / "ru"
    user.mkdir(parents=True)
    (user / "custom.ftl").write_text(TEMPLATE, "utf8")
    return user.parent, defaults


@pytest.fixture
def storage(dirs: tuple[Path, Path]) -> SLayeredFileStorage:
    return SLayeredFileStorage(*dirs)


@pytest.fixture
def texts(storage: SLayeredFileStorage) -> TextOverrides:
    config: Any = type("Config", (), {"default_locale": "ru"})()
    return TextOverrides(config, _Hub(storage))  # type: ignore[arg-type]


def shown(storage: SLayeredFileStorage, key: str, **kwargs: Any) -> str:
    text, errors = storage.get_translator("ru").translator.format(key, kwargs)  # type: ignore[union-attr]
    assert errors == []
    return str(text)


class TestRoundTrip:
    def test_every_text_reads_back_as_itself(self, texts: TextOverrides) -> None:
        # What the admin is shown and copies must parse into the same text.
        for entry in texts.entries():
            assert pattern_text(parse_text(entry.text)) == entry.text, entry.key

    def test_the_merged_texts_match_upstream(self, dirs: tuple[Path, Path]) -> None:
        old = LayeredFileStorage(*dirs).get_translator("ru")
        new = SLayeredFileStorage(*dirs).get_translator("ru")
        keys = (
            MAINTENANCE,
            "btn-menu.connect",
            "btn-s.section",
            "msg-menu-devices-confirm-reissue",
        )
        for key in keys:
            assert old.translator.format(key, {}) == new.translator.format(key, {})  # type: ignore[union-attr]


class TestSearch:
    def test_by_a_phrase_from_the_text(self, texts: TextOverrides) -> None:
        assert [e.key for e in texts.search("на обслуживании")] == [MAINTENANCE]

    def test_by_the_key(self, texts: TextOverrides) -> None:
        assert MAINTENANCE in [e.key for e in texts.search("access.maint")]

    def test_case_and_spacing_do_not_matter(self, texts: TextOverrides) -> None:
        assert [e.key for e in texts.search("  НА   обслуживании ")] == [MAINTENANCE]

    def test_nothing_for_an_empty_query(self, texts: TextOverrides) -> None:
        assert texts.search("   ") == []

    def test_variables_include_those_of_texts_it_uses(self, texts: TextOverrides) -> None:
        # msg-main-menu shows { frg-user }, which needs $telegram_id.
        assert {"status", "telegram_id"} <= texts.entry("msg-main-menu").variables


class TestCheck:
    async def test_a_good_text_is_rendered_with_sample_values(self, texts: TextOverrides) -> None:
        preview = await texts.check("msg-menu-devices", "Подключено: { $current_count }")
        assert preview == "Подключено: ‹current_count›"

    async def test_a_number_where_one_is_due(self, texts: TextOverrides) -> None:
        # frg-user formats $telegram_id with NUMBER(): a string would not do.
        preview = await texts.check("msg-main-menu", "{ frg-user }")
        assert "<code>1</code>" in preview

    async def test_it_may_use_a_built_in_text(self, texts: TextOverrides) -> None:
        preview = await texts.check(MAINTENANCE, "Работы. { btn-menu.connect }")
        assert preview == "Работы. 🚀 Подключиться"

    @pytest.mark.parametrize(
        "text, reason",
        [
            ("Привет { $nope }", "variable"),
            # frg-user needs $telegram_id, which this notice is never given.
            ("{ frg-user }", "variable"),
            ("{ frg-nope }", "render"),
            ("Скобка { не закрыта", "syntax"),
            ("[x] строка", "syntax"),
            (".attr = строка", "syntax"),
            ("  \n ", "empty"),
        ],
    )
    async def test_what_would_break_is_refused(
        self, texts: TextOverrides, text: str, reason: str
    ) -> None:
        with pytest.raises(TextError) as refused:
            await texts.check(MAINTENANCE, text)
        assert refused.value.reason == reason

    async def test_an_unknown_key(self, texts: TextOverrides) -> None:
        with pytest.raises(TextError) as refused:
            await texts.check("msg-nope", "x")
        assert refused.value.reason == "unknown"


class TestSave:
    async def test_saved_text_is_served_at_once(
        self, texts: TextOverrides, storage: SLayeredFileStorage
    ) -> None:
        await texts.save(ADMIN, MAINTENANCE, "🚧 Работы до <b>18:00</b>\nПотом заходите")

        assert shown(storage, MAINTENANCE) == "🚧 Работы до <b>18:00</b>\nПотом заходите"
        # The other attributes of ntf-access stay built-in.
        assert "Регистрация" in shown(storage, "ntf-access.registration-disabled")
        assert [e.key for e in texts.changed()] == [MAINTENANCE]

    async def test_custom_ftl_keeps_its_comments_and_reads_like_a_hand_edit(
        self, texts: TextOverrides
    ) -> None:
        await texts.save(ADMIN, MAINTENANCE, "строка один\nстрока два")

        written = texts.path.read_text("utf8")
        assert written.startswith(TEMPLATE.split("\n")[0])
        assert "custom-plan-name1" in written
        assert written.rstrip().endswith(
            "ntf-access =\n    .maintenance =\n        строка один\n        строка два"
        )

    async def test_a_second_save_replaces_the_first(self, texts: TextOverrides) -> None:
        await texts.save(ADMIN, MAINTENANCE, "первый")
        await texts.save(ADMIN, MAINTENANCE, "второй")
        assert texts.entry(MAINTENANCE).text == "второй"
        assert texts.path.read_text("utf8").count("ntf-access =") == 1

    async def test_a_broken_text_is_not_written(self, texts: TextOverrides) -> None:
        with pytest.raises(TextError):
            await texts.save(ADMIN, MAINTENANCE, "{ $nope }")
        assert texts.path.read_text("utf8") == TEMPLATE

    async def test_only_an_admin_with_settings_access(self, texts: TextOverrides) -> None:
        with pytest.raises(PermissionDeniedError):
            await texts.save(NOBODY, MAINTENANCE, "x")
        with pytest.raises(PermissionDeniedError):
            await texts.reset(NOBODY, MAINTENANCE)
        assert texts.path.read_text("utf8") == TEMPLATE

    async def test_reset_brings_the_built_in_text_back(
        self, texts: TextOverrides, storage: SLayeredFileStorage
    ) -> None:
        await texts.save(ADMIN, MAINTENANCE, "x")
        await texts.reset(ADMIN, MAINTENANCE)

        assert "обслуживании" in shown(storage, MAINTENANCE)
        assert texts.changed() == []
        # The comments are all there; only blank lines between them may differ.
        assert texts.path.read_text("utf8").split() == TEMPLATE.split()


class TestOtherProcesses:
    """The worker and the scheduler pick an edit up from the file."""

    def wait_reloaded(self, storage: SLayeredFileStorage, key: str, expected: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            storage._checked = 0.0  # skip the throttle
            storage.get_translators_for_language("ru")
            if shown(storage, key) == expected:
                return
            time.sleep(0.05)
        pytest.fail(f"'{key}' was not reloaded")

    def test_a_change_to_the_file_is_served_without_a_restart(
        self, texts: TextOverrides, storage: SLayeredFileStorage
    ) -> None:
        other = SLayeredFileStorage(storage._user_dir, storage._default_dir)
        texts_module.write_override(texts.path, MAINTENANCE, parse_text("из другого процесса"))
        self.wait_reloaded(other, MAINTENANCE, "из другого процесса")

    def test_a_broken_hand_edit_keeps_the_texts_from_before(
        self, storage: SLayeredFileStorage, dirs: tuple[Path, Path]
    ) -> None:
        before = shown(storage, MAINTENANCE)
        path = storage.custom_path("ru")
        path.write_text(TEMPLATE + "\nntf-access =\n    .maintenance = { $\n", "utf8")
        storage._checked = 0.0
        storage.get_translators_for_language("ru")
        deadline = time.monotonic() + 15
        while storage._building and time.monotonic() < deadline:
            time.sleep(0.05)
        assert shown(storage, MAINTENANCE) == before

    def test_the_throttle_skips_a_look_at_the_file(self, storage: SLayeredFileStorage) -> None:
        storage._checked = time.monotonic()
        texts_module.write_override(storage.custom_path("ru"), MAINTENANCE, parse_text("x"))
        storage.get_translators_for_language("ru")
        assert storage._building == set()


def test_custom_keys_of_the_admin_stay_available(storage: SLayeredFileStorage) -> None:
    # Plan names and menu buttons refer to keys of the template's own.
    assert shown(storage, "custom-plan-name1") == "1️⃣ Первый план"
