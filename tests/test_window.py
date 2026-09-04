"""The changes window, exercised on a real ``ChangesWindow`` under ``pytest-qt``.

Everything here goes through a Qt signal, an event or a public method rather than reaching past them, so a binding
that stops firing shows up as a failure here rather than as a control that silently does nothing.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt
from pytestqt.qtbot import QtBot

from gh_tray import popup, theme, window


def row(
    number: str = "#7",
    seen: bool = False,
    role: str = "",
    status: str = "",
    at: str = "2026-01-02T00:00:00.000000Z",
) -> popup.Row:
    """Build one row of the table."""
    return popup.Row(
        label="Checks broke",
        repo="acme/widget",
        number=number,
        title="Add a widget",
        who="someone",
        when="just now",
        url=f"https://example.test/{number.lstrip('#')}",
        colour=popup.URGENT,
        at=at,
        seen=seen,
        role=role,
        status=status,
    )


# Two rows, the first stamped later than the second, so the default newest-first sort is deterministic: "#7" leads.
ROWS = [
    row("#7", role="author", at="2026-01-02T00:00:00.000000Z"),
    row("#8", seen=True, role="reviewer", at="2026-01-01T00:00:00.000000Z"),
]


class WindowBuilder:
    """Builds ``ChangesWindow`` instances against one shared layout store, recording what each one does.

    Every window built through one instance shares the same layout file, so a test proving that one window's
    remembered size is picked up by another can build a second window and see it.
    """

    def __init__(self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, layout_path: Path) -> None:
        """:param layout_path: where every window this builds keeps its remembered sizes."""
        self._qtbot = qtbot
        self._monkeypatch = monkeypatch
        self._layout_path = layout_path
        self.opened: list[str] = []
        self.seen_marks: list[tuple[popup.Row, bool]] = []
        monkeypatch.setattr(window.webbrowser, "open", self.opened.append)
        monkeypatch.setattr(window, "remember_row_seen", lambda entry, seen: self.seen_marks.append((entry, seen)))

    def __call__(self, rows: list[popup.Row] | None = None) -> window.ChangesWindow:
        """Build one window, showing the given rows or the default two.

        :param rows: the rows to show, defaulting to :data:`ROWS`
        """
        chosen = list(rows if rows is not None else ROWS)
        self._monkeypatch.setattr(window, "rows_to_show", lambda _count: list(chosen))
        self._monkeypatch.setattr(window, "load_config", lambda: {"popup_rows": len(chosen)})
        settings = QSettings(str(self._layout_path), QSettings.Format.IniFormat)
        view = window.ChangesWindow(chosen, layout=settings)
        self._qtbot.addWidget(view)
        return view


@pytest.fixture
def build_window(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WindowBuilder:
    """Return a builder for ``ChangesWindow`` instances sharing one temporary layout store."""
    return WindowBuilder(qtbot, monkeypatch, tmp_path / "layout.ini")


@pytest.fixture
def view(build_window):
    """Return a window built against the default two rows."""
    return build_window()


def cell_centre(view: window.ChangesWindow, row_index: int, column: int = 0) -> QPoint:
    """Return the middle of one cell, in the table viewport's own coordinates."""
    return view.table.visualRect(view.table.model().index(row_index, column)).center()


def test_escape_hides_the_window(view, qtbot):
    view.show()
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert not view.isVisible()


def settle(view: window.ChangesWindow) -> None:
    """Show the window and age it past its settling time, so a loss of focus counts as the user's doing."""
    view.show()
    view.shown_at = time.monotonic() - window.FOCUS_SETTLE_SECONDS


def test_losing_the_focus_once_settled_puts_the_window_away(view, qapp):
    settle(view)
    qapp.sendEvent(view, QEvent(QEvent.Type.WindowDeactivate))
    assert not view.isVisible()


def test_a_desktop_drag_survives_losing_the_activation(view, qapp):
    # Some desktops, GNOME among them, take the activation for the whole of a move or resize they do on the
    # window's behalf, and hiding on that loss took the window away the moment an edge was dragged.
    settle(view)
    view.desktop_dragging = True
    qapp.sendEvent(view, QEvent(QEvent.Type.WindowDeactivate))
    assert view.isVisible(), "losing the activation to the desktop's own drag must not put the window away"
    qapp.sendEvent(view, QEvent(QEvent.Type.WindowActivate))
    qapp.sendEvent(view, QEvent(QEvent.Type.WindowDeactivate))
    assert not view.isVisible(), "a loss of focus after the drag is the user's doing again"


def test_a_refused_desktop_resize_leaves_the_dismissal_armed(view):
    # The offscreen platform refuses to resize on the application's behalf, as a desktop without the protocol does.
    settle(view)
    view.start_system_resize(Qt.Edge.RightEdge)
    assert not view.desktop_dragging


def test_close_hides_rather_than_destroys(view):
    view.show()
    view.close()
    assert not view.isVisible()
    # A destroyed widget raises on any further access; this only survives if it was merely hidden.
    assert view.table.rowCount() == len(ROWS)
    view.show()
    assert view.isVisible()


def test_a_left_click_on_a_row_opens_its_url_and_hides(view, qtbot, build_window):
    view.show()
    url = view.entries[0].url
    qtbot.mouseClick(view.table.viewport(), Qt.MouseButton.LeftButton, pos=cell_centre(view, 0))
    assert build_window.opened == [url]
    assert not view.isVisible()


def test_a_click_below_the_last_row_opens_nothing(view, qtbot, build_window):
    view.resize(900, 500)
    view.show()
    below = QPoint(10, view.table.viewport().height() - 2)
    qtbot.mouseClick(view.table.viewport(), Qt.MouseButton.LeftButton, pos=below)
    assert build_window.opened == []
    assert view.isVisible()


def test_a_right_click_marks_a_row_seen_and_a_second_click_unmarks_it_and_the_title_count_follows(view, qtbot):
    view.show()
    assert view.windowTitle().endswith("1 notification")
    spot = cell_centre(view, 0)
    qtbot.mouseClick(view.table.viewport(), Qt.MouseButton.RightButton, pos=spot)
    assert view.entries[0].seen is True
    assert view.windowTitle().endswith("nothing to do")
    qtbot.mouseClick(view.table.viewport(), Qt.MouseButton.RightButton, pos=spot)
    assert view.entries[0].seen is False
    assert view.windowTitle().endswith("1 notification")


def test_marking_is_remembered_through_remember_row_seen(view, qtbot, build_window):
    view.show()
    qtbot.mouseClick(view.table.viewport(), Qt.MouseButton.RightButton, pos=cell_centre(view, 0))
    marked_row, seen = build_window.seen_marks[-1]
    assert (marked_row.url, seen) == (view.entries[0].url, True)


def test_a_heading_click_sorts_and_a_second_click_reverses(view):
    view.table.horizontalHeader().sectionClicked.emit(window.column_of("pr"))
    assert [entry.number for entry in view.entries] == ["#7", "#8"]
    assert view.table.horizontalHeader().sortIndicatorSection() == window.column_of("pr")
    view.table.horizontalHeader().sectionClicked.emit(window.column_of("pr"))
    assert [entry.number for entry in view.entries] == ["#8", "#7"]


def test_quick_filters_narrow_rows_and_switching_filters_keeps_a_mark(view):
    view.chips["author"].click()
    assert [entry.number for entry in view.entries] == ["#7"]
    view.set_seen(0, True)
    view.chips["all"].click()
    assert next(entry.seen for entry in view.entries if entry.number == "#7") is True


def test_show_closed_toggles_finished_rows(build_window):
    finished = row("#9", role="mention", status="merged", at="2026-01-03T00:00:00.000000Z")
    view = build_window([*ROWS, finished])
    assert [entry.number for entry in view.entries] == ["#7", "#8"]
    view.closed_chip.setChecked(True)
    assert "#9" in [entry.number for entry in view.entries]
    view.closed_chip.setChecked(False)
    assert [entry.number for entry in view.entries] == ["#7", "#8"]


def test_a_dragged_column_width_is_remembered_and_used_by_a_fresh_window(build_window):
    first = build_window()
    first.show()
    column = window.column_of("repo")
    first.table.horizontalHeader().resizeSection(column, 260)
    assert first.remembered(window.COLUMN_KEY.format("repo")) > 0
    first.layout_store.sync()
    second = build_window()
    assert second.table.columnWidth(column) == 260


def test_a_user_resize_is_remembered_but_show_bys_own_width_is_not(view, qapp):
    # Widths are remembered in characters of the window's font rather than in pixels, so they come out the same
    # size on a display drawing at another scale; the round trip through characters() is what a fresh window uses.
    view.show_by(QPoint(400, 400))
    qapp.processEvents()
    assert view.remembered(window.WIDTH_KEY) == 0
    view.resize(view.width() + 40, view.height())
    assert view.characters(view.remembered(window.WIDTH_KEY)) == view.width()


def test_a_window_with_few_rows_is_snug(view, qapp):
    usable = view.usable_screen(QPoint(0, 0))
    view.show_by(QPoint(usable.right() - 10, usable.bottom() - 10))
    qapp.processEvents()
    assert view.height() == view.wanted_height(usable)
    assert view.height() < int(usable.height() * window.TALLEST_SHARE_OF_SCREEN)


def test_many_rows_are_capped_at_the_ceiling(build_window, qapp):
    view = build_window([row(f"#{n}", at=f"2026-01-{(n % 28) + 1:02d}T00:00:00.000000Z") for n in range(60)])
    usable = view.usable_screen(QPoint(0, 0))
    view.show_by(QPoint(usable.right() - 10, usable.bottom() - 10))
    qapp.processEvents()
    assert view.height() <= int(usable.height() * window.TALLEST_SHARE_OF_SCREEN)


def test_show_by_places_the_window_above_left_of_the_point_and_on_the_screen(view, qapp):
    # The button strip's own minimum width can exceed a small offscreen screen, so the window is held to the screen's
    # left and top edges first, and to its right and bottom edges as far as its size allows. The width and height are
    # the ones show_by asks for, since the toolkit may hold the window to its floor.
    usable = view.usable_screen(QPoint(0, 0))
    spot = QPoint(usable.left() + 700, usable.top() + 400)
    width, height = view.wanted_width(usable), view.wanted_height(usable)
    expected_left = max(
        usable.left() + window.EDGE_MARGIN,
        min(spot.x() - width + window.POINTER_OFFSET, usable.right() - width - window.EDGE_MARGIN),
    )
    expected_top = max(
        usable.top() + window.EDGE_MARGIN,
        min(spot.y() - height - window.POINTER_GAP, usable.bottom() - height - window.EDGE_MARGIN),
    )
    view.show_by(spot)
    qapp.processEvents()
    assert (view.geometry().x(), view.geometry().y()) == (expected_left, expected_top)
    assert view.geometry().height() == height


def test_refit_height_after_a_filter_change_keeps_the_bottom_edge(build_window, qapp):
    view = build_window([*ROWS, row("#9", role="mention", at="2026-01-03T00:00:00.000000Z")])
    usable = view.usable_screen(QPoint(0, 0))
    view.show_by(QPoint(usable.right() - 10, usable.bottom() - 10))
    qapp.processEvents()
    bottom = view.geometry().bottom()
    view.chips["author"].click()
    qapp.processEvents()
    assert view.geometry().bottom() == bottom


def test_refresh_emits_refresh_asked_and_disables_the_button(view, qtbot):
    with qtbot.waitSignal(view.refresh_asked, timeout=1000):
        view.refresh()
    assert view.refresh_button.isEnabled() is False
    assert view.refresh_button.text() == "Refreshing"


def test_on_polled_restores_the_button_and_hint_after_a_refresh(view):
    view.refresh()
    view.on_polled(True)
    assert view.refresh_button.isEnabled() is True
    assert view.refresh_button.text() == "Refresh"
    assert view.hint.text() == "Up to date."


def test_toggle_shows_then_hides(view, qapp):
    assert not view.isVisible()
    view.toggle(QPoint(100, 100))
    qapp.processEvents()
    assert view.isVisible()
    view.toggle(QPoint(100, 100))
    assert not view.isVisible()


def test_on_scheme_changed_repaints_without_error(view):
    view.on_scheme_changed()
    assert view.inks is not None


def test_rows_from_the_same_organisation_or_repository_share_a_colour(build_window, qapp):
    # Both unseen, since a row already seen is dimmed and so drawn a shade apart.
    view = build_window([row("#7", role="author"), row("#8", role="reviewer")])
    view.show_by(QPoint(400, 400))
    qapp.processEvents()
    org, repo = window.column_of("org"), window.column_of("repo")
    first, second = view.entries[0], view.entries[1]
    assert first.repo.split("/")[0] == second.repo.split("/")[0]
    assert view.table.item(0, org).foreground().color() == view.table.item(1, org).foreground().color()
    owner = first.repo.split("/")[0]
    assert view.table.item(0, org).foreground().color().name() == theme.ink(view.inks, popup.name_colour(owner))
    assert view.table.item(0, repo).foreground().color().name() == theme.ink(view.inks, popup.name_colour(first.repo))
