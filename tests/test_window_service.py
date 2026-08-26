"""Asking the waiting window to show itself, and not starting a second one.

The window stays loaded and hidden in a process of its own, so a click on the tray icon leaves it a note rather
than starting anything. The drawing is not exercised here: what matters is that any number of clicks produce one
note and at most one window process.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pystray", reason="the tray icon needs a display library")

from gh_tray import popup, tray
from gh_tray.environment import SingleInstance


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Point the note and the lock at a temporary directory."""
    monkeypatch.setattr(popup, "POPUP_REQUEST_PATH", tmp_path / "popup.request")
    monkeypatch.setattr(popup, "POPUP_LOCK_PATH", tmp_path / "popup.lock")
    return tmp_path


class Clicks:
    """A stand-in tray carrying only what clicking the icon touches."""

    on_popup = tray.Tray.on_popup

    def __init__(self) -> None:
        """Start with no window process."""
        self.window = None


@pytest.fixture
def clicks(paths, monkeypatch):
    """Build a stand-in tray that records how many window processes it would have started."""
    subject = Clicks()
    subject.started = []

    def start_window():
        """Stand in for starting the window process."""
        subject.started.append("a window")
        return subject.started[-1]

    monkeypatch.setattr(tray, "start_window", start_window)
    return subject


def test_nothing_is_waiting_when_no_window_holds_the_lock(paths):
    assert popup.window_waiting() is False


def test_a_window_holding_the_lock_is_seen_as_waiting(paths):
    held = SingleInstance(popup.POPUP_LOCK_PATH)
    assert held.acquire()
    try:
        assert popup.window_waiting() is True
    finally:
        held.release()


def test_looking_for_a_window_does_not_take_the_lock_from_the_next_looker(paths):
    assert popup.window_waiting() is False
    assert popup.window_waiting() is False


def test_asking_leaves_a_note(paths):
    popup.request_popup()
    assert popup.POPUP_REQUEST_PATH.exists()


def test_many_clicks_leave_one_note(paths):
    for _click in range(5):
        popup.request_popup()
    assert popup.POPUP_REQUEST_PATH.exists()


def test_a_window_is_started_when_none_is_waiting(clicks):
    clicks.on_popup()
    assert clicks.started == ["a window"]
    assert popup.POPUP_REQUEST_PATH.exists()


def test_no_window_is_started_while_one_is_waiting(clicks):
    held = SingleInstance(popup.POPUP_LOCK_PATH)
    assert held.acquire()
    try:
        for _click in range(4):
            clicks.on_popup()
    finally:
        held.release()
    assert clicks.started == []
    assert popup.POPUP_REQUEST_PATH.exists()
