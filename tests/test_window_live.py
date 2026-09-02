"""The window's own controls, exercised on a real window.

Everything here goes through a binding or a note rather than calling the method behind it. A binding that names
something no longer there fails silently: the toolkit reports it on an error stream that a windowed process does
not have, so the control simply stops working and nothing says why. That is what these cover.

One window serves every test here. Building a second in the same process fails on some toolkit builds, and there is
nothing about these controls that needs a window of its own. Building any window needs a display, so all of this is
skipped where there is none.
"""

from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace

import pytest

pytest.importorskip("tksheet", reason="the window needs its table widget")

from gh_tray import popup, window

ROWS = [
    popup.Row(
        label="Checks broke",
        repo="acme/widget",
        number=number,
        title="Add a widget",
        who="someone",
        when="just now",
        url=f"https://example.test/{number.lstrip('#')}",
        colour=popup.URGENT,
        at="2026-01-01T00:00:00.000000Z",
        role="author" if number == "#7" else "reviewer",
    )
    for number in ("#7", "#8")
]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build one real window against made-up rows, or skip where there is no display to build it on."""
    patch = pytest.MonkeyPatch()
    holding = tmp_path_factory.mktemp("window")
    patch.setattr(window, "POPUP_REQUEST_PATH", holding / "popup.request")
    patch.setattr(popup, "POPUP_REQUEST_PATH", holding / "popup.request")
    patch.setattr(popup, "LAYOUT_PATH", holding / "layout.json")
    patch.setattr(window, "rows_to_show", lambda _count: list(ROWS))
    patch.setattr(window, "load_config", lambda: {"popup_rows": len(ROWS)})
    patch.setattr(window, "remember_row_seen", lambda *_arguments: None)
    try:
        view = window.Popup(list(ROWS))
    except tk.TclError as error:
        patch.undo()
        pytest.skip(f"no display to build a window on: {error}")
    view.edge_handles()
    view.root.bind("<Escape>", view.hide)
    view.root.bind("<FocusOut>", view.on_focus_out)
    # The window asks where the focus went before putting itself away. A test runner rarely holds the focus at all,
    # which would answer "somewhere else" and dismiss the window in the middle of whatever is being tested, so the
    # answer is fixed here and the tests that care about losing it say so themselves.
    patch.setattr(view.root, "focus_displayof", lambda: view.root)
    yield view
    view.root.destroy()
    patch.undo()


@pytest.fixture
def view(built):
    """Return the window put away and forgetful of whatever the last test did to it."""
    built.hide()
    built.dismissed_at = None
    window.POPUP_REQUEST_PATH.unlink(missing_ok=True)
    popup.LAYOUT_PATH.unlink(missing_ok=True)
    built.root.update()
    return built


def shown(view: window.Popup) -> bool:
    """Return whether the window is actually on screen."""
    view.root.update()
    return bool(view.root.winfo_viewable())


def bring_up(view: window.Popup) -> None:
    """Show the window and treat it as having finished arriving.

    The window decides that for itself on a timer. Taking the timer away and saying so directly keeps these tests
    off the clock, and stops a timer outliving the test that started it.
    """
    view.show()
    view.stop_settling()
    view.settle()
    view.root.update()


def lose_the_focus(view: window.Popup, monkeypatch) -> None:
    """Take the focus away to another application, as clicking one does."""
    monkeypatch.setattr(view.root, "focus_displayof", lambda: None)
    view.on_focus_out()
    view.root.update()
    view.root.update_idletasks()


def ask_for_it(view: window.Popup, spot: tuple[int, int] | None = None) -> None:
    """Leave the note the tray leaves, and let the window answer it."""
    popup.request_popup(spot)
    view.watch()
    view.root.update()


def test_the_close_mark_puts_the_window_away(view):
    bring_up(view)
    assert shown(view)
    view.close_mark.event_generate("<Button-1>")
    assert not shown(view)


def test_escape_puts_the_window_away(view):
    bring_up(view)
    view.root.event_generate("<Escape>")
    assert not shown(view)


def test_opening_a_row_puts_the_window_away(view, monkeypatch):
    opened = []
    monkeypatch.setattr(window.webbrowser, "open", opened.append)
    bring_up(view)
    view.sheet.MT.event_generate("<Button-1>", x=60, y=view.row_height() // 2, time=5000)
    view.root.update()
    assert opened == [view.entries[0].url]
    assert not shown(view)


def test_being_asked_for_shows_the_window(view):
    ask_for_it(view)
    assert shown(view)
    assert not window.POPUP_REQUEST_PATH.exists(), "the note should be taken, or the window would show again forever"


def test_a_window_put_away_comes_back_when_asked_again(view):
    bring_up(view)
    view.hide()
    ask_for_it(view)
    assert shown(view)


def test_clicking_the_icon_while_the_window_is_up_puts_it_away(view):
    # Some desktops take the focus from the window when the tray icon is clicked, and some do not, so the window
    # may still be up when the note arrives. Either way the click means put it away.
    bring_up(view)
    ask_for_it(view)
    assert not shown(view), "the window stayed up, so the click did nothing"


def test_a_click_that_dismissed_the_window_does_not_fetch_it_back(view, monkeypatch):
    bring_up(view)
    # Where the click does take the focus, the window is already away by the time the note arrives.
    lose_the_focus(view, monkeypatch)
    assert not shown(view)
    ask_for_it(view)
    assert not shown(view), "the click that dismissed the window brought it straight back"


def test_a_later_click_still_shows_the_window(view, monkeypatch):
    bring_up(view)
    lose_the_focus(view, monkeypatch)
    monkeypatch.setattr(window.time, "monotonic", lambda: view.dismissed_at + window.TOGGLE_WITHIN_SECONDS + 1)
    ask_for_it(view)
    assert shown(view)


def test_clicking_inside_the_window_does_not_dismiss_it(view):
    # Clicking a heading hands the focus to the table, which the toolkit reports exactly as it reports the focus
    # leaving for another application. Sorting a column used to make the window vanish.
    bring_up(view)
    view.sheet.CH.event_generate("<Button-1>", x=60, y=view.row_height() // 2, time=5000)
    view.root.update()
    view.root.update_idletasks()
    assert shown(view), "the window went away when the focus moved inside it"


def test_a_display_drawing_at_a_different_size_has_the_window_built_again(view, monkeypatch):
    # The text follows the display's scaling by itself; the column widths and row heights measured from it do not,
    # so they have to be worked out again. A locked screen coming back differently is enough to need this.
    was_tall, was_table = view.built_for, view.sheet
    finer = 2 * (window.pointer_scaling() or 96)
    monkeypatch.setattr(window, "pointer_scaling", lambda: finer)
    bring_up(view)
    assert view.built_for > was_tall, "the rows should have grown with the display"
    assert view.sheet is not was_table, "the table was left at the size it was first built for"
    assert shown(view)


def test_losing_the_focus_while_arriving_does_not_dismiss_the_window(view):
    view.show()
    view.root.update()
    view.on_focus_out()
    assert shown(view), "the window dismissed itself before it had finished appearing"


def test_a_dragged_width_is_remembered_and_a_dragged_height_is_not(view):
    # The height always follows the rows, so keeping a dragged one would only ever add blank table or hide rows.
    bring_up(view)
    snug = view.root.winfo_height()
    view.start_drag(SimpleNamespace(x_root=0, y_root=0))
    view.resize_edges(SimpleNamespace(x_root=90, y_root=70), (False, False, True, True))
    view.root.update()
    view.on_resize_finished()
    dragged_wide = view.root.winfo_width()
    assert popup.remembered_width(view.dots) == dragged_wide
    view.hide()
    bring_up(view)
    assert view.root.winfo_width() == dragged_wide
    assert view.root.winfo_height() == snug, "the height should have gone back to hugging the rows"


def test_a_click_on_an_edge_that_moves_nothing_remembers_nothing(view):
    bring_up(view)
    view.start_drag(SimpleNamespace(x_root=0, y_root=0))
    view.on_resize_finished()
    assert popup.remembered_width(view.dots) is None


def test_a_dragged_column_width_is_remembered_and_used_next_time(view):
    bring_up(view)
    view.sheet.set_column_widths(iter([260, 140, 160, 60, 80, 260, 120, 120, 90]))
    view.on_column_dragged()
    assert popup.remembered_column_widths(view.dots)["change"] == 260
    # A fresh window, as after a restart, starts at the dragged widths rather than the stated ones.
    view.suit_the_display()
    view.sheet.set_column_widths(iter(view.column_widths()))
    assert round(view.sheet.get_column_widths()[0]) == 260


def snug_row(number: int) -> popup.Row:
    """Build one row for the height tests."""
    return popup.Row(
        "New comment",
        "acme/widgets",
        f"#{number}",
        "Add a widget",
        "alice",
        "1h ago",
        f"u{number}",
        popup.URGENT,
        at="2026-01-01T00:00:00.000000Z",
    )


def test_a_quiet_day_gets_a_window_snug_around_its_rows(view, monkeypatch):
    # The table widget asks for a fixed minimum height of its own, which used to pad the window with empty table.
    monkeypatch.setattr(window, "rows_to_show", lambda _count: [snug_row(n) for n in range(3)])
    bring_up(view)
    assert view.sheet.winfo_height() == view.table_height(), "the table should hold exactly its rows"
    assert view.hint.winfo_viewable(), "the bottom strip should survive the snug height"


def test_many_rows_grow_the_window_only_to_its_ceiling(view, monkeypatch):
    monkeypatch.setattr(window, "rows_to_show", lambda _count: [snug_row(n) for n in range(60)])
    bring_up(view)
    _width, usable_height = view.usable_screen()
    assert view.root.winfo_height() <= int(usable_height * window.TALLEST_SHARE_OF_SCREEN)
    assert view.sheet.winfo_height() < view.table_height(), "sixty rows should overflow into scrolling, not height"
    assert view.hint.winfo_viewable(), "the ceiling must squeeze the table, never the bottom strip"


def test_the_window_comes_up_by_the_click_not_the_pointer(view):
    # The window comes up half a second after the click that asked for it, and used to follow wherever the pointer
    # had wandered to in between. A spot near the bottom right, where a tray icon lives, leaves it room to open.
    usable_width, usable_height = view.usable_screen()
    spot = (usable_width - 40, usable_height - 40)
    ask_for_it(view, spot=spot)
    assert shown(view)
    assert abs((view.root.winfo_x() + view.root.winfo_width() - window.POINTER_OFFSET) - spot[0]) <= 2
    assert abs((view.root.winfo_y() + view.root.winfo_height() + window.POINTER_GAP) - spot[1]) <= 2


def test_a_note_without_a_spot_still_shows_the_window(view):
    ask_for_it(view, spot=None)
    assert shown(view)


def test_the_quick_filters_narrow_the_table_without_moving_the_window(view):
    bring_up(view)
    stood = (view.root.winfo_x(), view.root.winfo_y() + view.root.winfo_height())
    view.chips["reviewer"].event_generate("<Button-1>")
    view.root.update()
    assert [entry.number for entry in view.entries] == ["#8"]
    where = (view.root.winfo_x(), view.root.winfo_y() + view.root.winfo_height())
    assert where == stood, "a filter click must keep the bottom edge where it is, not teleport the window"
    view.chips["all"].event_generate("<Button-1>")
    view.root.update()
    assert len(view.entries) == 2


def test_a_mark_made_under_one_filter_survives_switching_to_another(view):
    bring_up(view)
    view.chips["author"].event_generate("<Button-1>")
    view.root.update()
    view.set_seen(0, True)
    view.chips["all"].event_generate("<Button-1>")
    view.root.update()
    assert [entry.seen for entry in view.entries if entry.number == "#7"] == [True]


def test_closed_rows_stay_hidden_until_the_toggle_asks_for_them(view, monkeypatch):
    finished = popup.Row(
        "Checks broke",
        "acme/widget",
        "#9",
        "Add a widget",
        "someone",
        "just now",
        "https://example.test/9",
        popup.URGENT,
        at="2026-01-01T00:00:00.000000Z",
        status="merged",
    )
    monkeypatch.setattr(window, "rows_to_show", lambda _count: [*ROWS, finished])
    bring_up(view)
    assert [entry.number for entry in view.entries] == ["#7", "#8"]
    view.closed_chip.event_generate("<Button-1>")
    view.root.update()
    assert "#9" in [entry.number for entry in view.entries]
    view.closed_chip.event_generate("<Button-1>")
    view.root.update()
    assert [entry.number for entry in view.entries] == ["#7", "#8"]


def test_extra_rows_grow_the_window_upward_from_its_resting_place_above_the_taskbar(view, monkeypatch):
    finished = [
        popup.Row(
            "Checks broke",
            "acme/widget",
            f"#{number}",
            "Add a widget",
            "someone",
            "just now",
            f"https://example.test/{number}",
            popup.URGENT,
            at="2026-01-01T00:00:00.000000Z",
            status="merged",
        )
        for number in range(20, 26)
    ]
    monkeypatch.setattr(window, "rows_to_show", lambda _count: [*ROWS, *finished])
    bring_up(view)
    # Sit the window just above the taskbar, where a click on the tray icon puts it.
    _usable_width, usable_height = view.usable_screen()
    view.root.geometry(f"+{view.root.winfo_x()}+{usable_height - view.root.winfo_height() - window.EDGE_MARGIN}")
    view.root.update()
    height = view.root.winfo_height()
    bottom = view.root.winfo_y() + height
    view.closed_chip.event_generate("<Button-1>")
    view.root.update()
    assert view.root.winfo_height() > height, "six more rows must make the window taller"
    assert view.root.winfo_y() + view.root.winfo_height() == bottom, (
        "the extra height must go upward, the bottom staying put"
    )
    view.closed_chip.event_generate("<Button-1>")
    view.root.update()
    assert view.root.winfo_y() + view.root.winfo_height() == bottom, (
        "shrinking back must leave the bottom where it was too"
    )
