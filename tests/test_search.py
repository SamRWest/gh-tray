"""The search box: typing narrows the rows live, over every column, and Ctrl+F gets the keyboard there."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt

from gh_tray import popup, window

ROWS = [
    popup.Row(
        "Checks broke",
        "acme/widget",
        "#7",
        "Add a widget",
        "alice",
        "1h ago",
        "https://example.test/7",
        popup.URGENT,
        author="SamRWest",
        role="author",
        status="open",
    ),
    popup.Row(
        "New comment",
        "acme/gadget",
        "#8",
        "Fix the flange",
        "bob",
        "2h ago",
        "https://example.test/8",
        popup.ROUTINE,
        author="emily",
        role="reviewer",
        status="draft",
    ),
    popup.Row(
        "Mentioned",
        "other/thing",
        "#9",
        "Widgets everywhere",
        "carol",
        "3h ago",
        "https://example.test/9",
        popup.ROUTINE,
        author="dave",
        role="mention",
        status="open",
    ),
]


@pytest.fixture
def view(qtbot, monkeypatch, tmp_path: Path) -> window.ChangesWindow:
    monkeypatch.setattr(window, "rows_to_show", lambda _count: list(ROWS))
    monkeypatch.setattr(window, "load_config", lambda: {"popup_rows": 20})
    monkeypatch.setattr(window, "remember_row_seen", lambda *_arguments: None)
    built = window.ChangesWindow(list(ROWS), QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(built)
    return built


def numbers(view: window.ChangesWindow) -> list[str]:
    return [entry.number for entry in view.entries]


def test_a_row_matches_text_in_any_column_whatever_the_case():
    assert popup.matches_search(ROWS[0], "WIDG")
    assert popup.matches_search(ROWS[0], "samr")
    assert popup.matches_search(ROWS[1], "draft")
    assert popup.matches_search(ROWS[2], "#9")
    assert not popup.matches_search(ROWS[1], "widget")
    assert popup.matches_search(ROWS[1], "")


def test_typing_narrows_the_rows_as_it_goes(view):
    view.search.setText("widg")
    assert numbers(view) == ["#7", "#9"]
    view.search.setText("gadget")
    assert numbers(view) == ["#8"]
    view.search.clear()
    assert numbers(view) == ["#7", "#8", "#9"]


def test_the_search_works_alongside_the_quick_filters(view):
    view.choose_filter("mention")
    view.search.setText("acme")
    assert numbers(view) == []
    view.choose_filter("all")
    assert numbers(view) == ["#7", "#8"]


def test_ctrl_f_puts_the_keyboard_in_the_search_box(view, qtbot, qapp):
    view.show_by(QPoint(400, 400))
    view.table.setFocus()
    qapp.processEvents()
    qtbot.keyClick(view, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: view.search.hasFocus())


def test_escape_clears_a_search_before_it_puts_the_window_away(view, qtbot, qapp):
    view.show_by(QPoint(400, 400))
    view.search.setText("acme")
    view.search.setFocus()
    qapp.processEvents()
    qtbot.keyClick(view.search, Qt.Key.Key_Escape)
    assert view.search.text() == ""
    assert view.isVisible()
    qtbot.keyClick(view.search, Qt.Key.Key_Escape)
    assert not view.isVisible()
