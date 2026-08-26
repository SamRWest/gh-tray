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
    )
    for number in ("#7", "#8")
]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build one real window against made-up rows, or skip where there is no display to build it on."""
    patch = pytest.MonkeyPatch()
    patch.setattr(window, "POPUP_REQUEST_PATH", tmp_path_factory.mktemp("window") / "popup.request")
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
    yield view
    view.root.destroy()
    patch.undo()


@pytest.fixture
def view(built):
    """Return the window put away and forgetful of whatever the last test did to it."""
    built.hide()
    built.dismissed_at = None
    window.POPUP_REQUEST_PATH.unlink(missing_ok=True)
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


def ask_for_it(view: window.Popup) -> None:
    """Leave the note the tray leaves, and let the window answer it."""
    window.POPUP_REQUEST_PATH.write_text("asked", encoding="utf-8")
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


def test_clicking_the_icon_while_the_window_is_up_puts_it_away_rather_than_fetching_it_back(view):
    bring_up(view)
    # Clicking the tray icon takes the focus from the window, which puts it away, and the note follows a moment later.
    view.on_focus_out()
    assert not shown(view)
    ask_for_it(view)
    assert not shown(view), "the click that dismissed the window brought it straight back"


def test_a_later_click_still_shows_the_window(view, monkeypatch):
    bring_up(view)
    view.on_focus_out()
    monkeypatch.setattr(window.time, "monotonic", lambda: view.dismissed_at + window.TOGGLE_WITHIN_SECONDS + 1)
    ask_for_it(view)
    assert shown(view)


def test_losing_the_focus_while_arriving_does_not_dismiss_the_window(view):
    view.show()
    view.root.update()
    view.on_focus_out()
    assert shown(view), "the window dismissed itself before it had finished appearing"
