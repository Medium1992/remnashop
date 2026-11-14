"""``when`` of ``I18nFormat`` is positional only.

Given by name, it lands among the placeholders: the text is always shown, and
with the placeholders it needs missing, Fluent fails the whole window.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_i18n_format_takes_when_by_name() -> None:
    wrong = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in ROOT.glob("src/**/*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "I18nFormat"
        and any(keyword.arg == "when" for keyword in node.keywords)
    ]
    assert not wrong
