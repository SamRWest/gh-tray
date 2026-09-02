"""Ctrl and the mouse wheel size the text, the size is remembered, and the window's rows and columns follow it."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QLabel, QWidget

from gh_tray import popup, toolkit, window

# Enough rows that the window stands well above its minimum height, so growing text has somewhere to show.
ROWS = [
    popup.Row(
        "Checks broke",
        "acme/widget",
        f"#{number}",
        "Add a widget",
        "alice",
        "1h ago",
        f"https://example.test/{number}",
        popup.URGENT,
        role="author",
    )
    for number in range(8)
]


def store_at(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def zoom(qapp, tmp_path):
    # Put back and stopped afterwards, so the tests that follow start from the platform's own font.
    zooming = toolkit.FontZoom(store_at(tmp_path))
    yield zooming
    zooming.reset()
    zooming.stop()


@pytest.fixture
def target(qtbot) -> QWidget:
    widget = QLabel("anything under the pointer")
    qtbot.addWidget(widget)
    return widget


def wheel(qapp, target: QWidget, notches: int, with_ctrl: bool = True) -> None:
    held = Qt.KeyboardModifier.ControlModifier if with_ctrl else Qt.KeyboardModifier.NoModifier
    for _ in range(abs(notches)):
        turn = QWheelEvent(
            QPointF(5, 5),
            QPointF(5, 5),
            QPoint(0, 0),
            QPoint(0, 120 if notches > 0 else -120),
            Qt.MouseButton.NoButton,
            held,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        qapp.sendEvent(target, turn)


def ctrl_zero(qapp, target: QWidget) -> None:
    qapp.sendEvent(target, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier))


def changes_window(monkeypatch, store: QSettings, qtbot) -> window.ChangesWindow:
    monkeypatch.setattr(window, "rows_to_show", lambda _count: list(ROWS))
    monkeypatch.setattr(window, "load_config", lambda: {"popup_rows": 20})
    monkeypatch.setattr(window, "remember_row_seen", lambda *_arguments: None)
    view = window.ChangesWindow(list(ROWS), store)
    qtbot.addWidget(view)
    return view


def test_the_wheel_with_ctrl_sizes_the_text_and_remembers_it(qapp, zoom, target):
    base = toolkit.base_font().pointSize()
    wheel(qapp, target, 2)
    assert qapp.font().pointSize() == base + 2
    assert zoom.store.value(toolkit.ZOOM_KEY, 0, int) == 2
    wheel(qapp, target, -1)
    assert qapp.font().pointSize() == base + 1


def test_ctrl_and_zero_put_the_text_back(qapp, zoom, target):
    wheel(qapp, target, 3)
    ctrl_zero(qapp, target)
    assert qapp.font().pointSize() == toolkit.base_font().pointSize()
    assert zoom.steps == 0


def test_the_wheel_without_ctrl_is_left_to_whatever_is_under_the_pointer(qapp, zoom, target):
    wheel(qapp, target, 2, with_ctrl=False)
    assert qapp.font().pointSize() == toolkit.base_font().pointSize()


def test_the_text_cannot_be_taken_past_the_readable_range(zoom):
    zoom.step(100)
    assert zoom.steps == toolkit.ZOOM_RANGE[1]
    zoom.step(-100)
    assert zoom.steps == toolkit.ZOOM_RANGE[0]


def test_a_remembered_zoom_is_taken_up_when_the_application_starts(qapp, tmp_path):
    store = store_at(tmp_path)
    store.setValue(toolkit.ZOOM_KEY, 3)
    zooming = toolkit.FontZoom(store)
    try:
        assert qapp.font().pointSize() == toolkit.base_font().pointSize() + 3
    finally:
        zooming.reset()
        zooming.stop()


def test_the_rows_and_columns_follow_the_text(qapp, qtbot, monkeypatch, tmp_path, zoom):
    view = changes_window(monkeypatch, zoom.store, qtbot)
    zoom.changed.connect(view.on_font_changed)
    view.show_by(QPoint(400, 400))
    qapp.processEvents()
    row, column, height = view.table.rowHeight(0), view.table.columnWidth(0), view.geometry().height()
    wheel(qapp, view.table.viewport(), 3)
    qapp.processEvents()
    assert view.table.rowHeight(0) > row
    assert view.table.columnWidth(0) > column
    assert view.geometry().height() > height
    ctrl_zero(qapp, view)
    # The columns are fitted again once the table has settled, which takes the toolkit a moment.
    qtbot.waitUntil(lambda: (view.table.rowHeight(0), view.table.columnWidth(0)) == (row, column))


def test_a_fresh_window_comes_up_at_the_remembered_zoom(qapp, qtbot, monkeypatch, tmp_path, zoom):
    first = changes_window(monkeypatch, zoom.store, qtbot)
    zoom.changed.connect(first.on_font_changed)
    first.show_by(QPoint(400, 400))
    wheel(qapp, first.table.viewport(), 2)
    qapp.processEvents()
    second = changes_window(monkeypatch, zoom.store, qtbot)
    second.show_by(QPoint(400, 400))
    qtbot.waitUntil(lambda: second.table.columnWidth(0) == first.table.columnWidth(0))
