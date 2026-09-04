"""The frameless window's own chrome: the title strip that moves it, the edges that resize it, and what puts it away."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt

from gh_tray import popup, window

ROW = popup.Row(
    "Checks broke",
    "acme/widget",
    "#7",
    "Add a widget",
    "alice",
    "1h ago",
    "https://example.test/7",
    popup.URGENT,
    role="author",
)


@pytest.fixture
def view(qtbot, monkeypatch, tmp_path: Path) -> window.ChangesWindow:
    monkeypatch.setattr(window, "rows_to_show", lambda _count: [ROW])
    monkeypatch.setattr(window, "load_config", lambda: {"popup_rows": 20})
    monkeypatch.setattr(window, "remember_row_seen", lambda *_arguments: None)
    built = window.ChangesWindow([ROW], QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(built)
    return built


@pytest.fixture
def shown(view, qapp, monkeypatch) -> window.ChangesWindow:
    view.show_by(QPoint(400, 400))
    qapp.processEvents()
    # Settled: the window came up long enough ago that a loss of focus is the user's doing.
    monkeypatch.setattr(window.time, "monotonic", lambda: view.shown_at + window.FOCUS_SETTLE_SECONDS + 1)
    return view


def deactivate(qapp, view) -> None:
    qapp.sendEvent(view, QEvent(QEvent.Type.WindowDeactivate))


def test_the_window_has_no_frame_and_stays_on_top(view):
    flags = view.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint


def test_the_close_mark_puts_the_window_away(shown, qtbot):
    qtbot.mouseClick(shown.close_mark, Qt.MouseButton.LeftButton)
    assert not shown.isVisible()


def test_the_title_strip_counts_the_rows_not_yet_seen(view):
    assert view.name.text() == "gh-tray - 1 notification"
    view.set_seen(0, True)
    assert view.name.text() == "gh-tray - nothing to do"


def test_losing_the_focus_puts_the_window_away(shown, qapp):
    deactivate(qapp, shown)
    assert not shown.isVisible()
    assert shown.dismissed_at is not None


def test_losing_the_focus_while_arriving_does_not(view, qapp):
    view.show_by(QPoint(400, 400))
    qapp.processEvents()
    deactivate(qapp, view)
    assert view.isVisible()


def test_the_click_that_dismissed_the_window_does_not_fetch_it_back(shown, qapp, monkeypatch):
    deactivate(qapp, shown)
    monkeypatch.setattr(window.time, "monotonic", lambda: shown.dismissed_at + window.TOGGLE_WITHIN_SECONDS / 2)
    shown.toggle(QPoint(400, 400))
    assert not shown.isVisible()


def test_a_later_click_shows_the_window_again(shown, qapp, monkeypatch):
    deactivate(qapp, shown)
    monkeypatch.setattr(window.time, "monotonic", lambda: shown.dismissed_at + window.TOGGLE_WITHIN_SECONDS * 2)
    shown.toggle(QPoint(400, 400))
    assert shown.isVisible()


def test_the_edges_are_found_from_a_point(shown):
    width, height = shown.width(), shown.height()
    assert shown.edges_at(QPoint(2, 2)) == Qt.Edge.LeftEdge | Qt.Edge.TopEdge
    assert shown.edges_at(QPoint(width - 2, height // 2)) == Qt.Edge.RightEdge
    assert shown.edges_at(QPoint(width // 2, height - 2)) == Qt.Edge.BottomEdge
    assert shown.edges_at(QPoint(width // 2, height // 2)) == Qt.Edge(0)


def test_a_press_on_the_title_strip_hands_the_desktop_a_move(shown, qtbot, monkeypatch):
    moves = []
    monkeypatch.setattr(shown, "start_system_move", lambda: moves.append(True))
    qtbot.mousePress(shown, Qt.MouseButton.LeftButton, pos=shown.strip.geometry().center())
    assert moves == [True]


def test_a_press_on_an_edge_hands_the_desktop_a_resize_by_that_edge(shown, qtbot, monkeypatch):
    resizes = []
    monkeypatch.setattr(shown, "start_system_resize", lambda edges: resizes.append(edges))
    qtbot.mousePress(shown, Qt.MouseButton.LeftButton, pos=QPoint(shown.width() - 2, shown.height() - 2))
    assert resizes == [Qt.Edge.RightEdge | Qt.Edge.BottomEdge]
