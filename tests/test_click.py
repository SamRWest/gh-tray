"""Telling one click on the tray icon from two.

The tray library reports every left click and has no notion of a double click, so the pair is recognised from
timing. A single click must not also fire as the first half of a double click, which is what makes this worth
testing rather than eyeballing.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("pystray", reason="the tray icon needs a display library")

from gh_tray import tray


class Recorder:
    """A stand-in tray that records which action each click sequence produced."""

    def __init__(self) -> None:
        """Start with no clicks recorded."""
        self.opened_dashboard = 0
        self.opened_popup = 0
        self.last_click: float | None = None
        self.pending_click: threading.Timer | None = None
        self.clock = 0.0

    on_click = tray.Tray.on_click

    def on_dashboard(self, *_) -> None:
        """Record that the dashboard was asked for."""
        self.opened_dashboard += 1

    def on_popup(self, *_) -> None:
        """Record that the change list was asked for."""
        self.opened_popup += 1


@pytest.fixture
def recorder(monkeypatch):
    """Build a recorder whose clock and timer the test controls rather than real time."""
    subject = Recorder()
    monkeypatch.setattr(tray.time, "monotonic", lambda: subject.clock)
    return subject


def run_pending(subject: Recorder) -> None:
    """Fire the delayed single-click action as the timer eventually would."""
    if subject.pending_click is not None:
        subject.pending_click.cancel()
        subject.pending_click.function()
        subject.pending_click = None


def test_one_click_shows_the_change_list(recorder):
    recorder.on_click()
    run_pending(recorder)
    assert (recorder.opened_popup, recorder.opened_dashboard) == (1, 0)


def test_two_quick_clicks_open_the_dashboard_and_not_the_change_list(recorder):
    recorder.on_click()
    recorder.clock += tray.DOUBLE_CLICK_SECONDS / 2
    recorder.on_click()
    run_pending(recorder)
    assert (recorder.opened_popup, recorder.opened_dashboard) == (0, 1)


def test_two_slow_clicks_are_two_single_clicks(recorder):
    recorder.on_click()
    run_pending(recorder)
    recorder.clock += tray.DOUBLE_CLICK_SECONDS * 3
    recorder.on_click()
    run_pending(recorder)
    assert (recorder.opened_popup, recorder.opened_dashboard) == (2, 0)


def test_a_third_quick_click_does_not_open_the_dashboard_twice(recorder):
    for _ in range(3):
        recorder.on_click()
        recorder.clock += tray.DOUBLE_CLICK_SECONDS / 2
    run_pending(recorder)
    assert recorder.opened_dashboard == 1


def test_the_delayed_action_is_cancelled_by_the_second_click(recorder):
    recorder.on_click()
    assert recorder.pending_click is not None
    recorder.clock += tray.DOUBLE_CLICK_SECONDS / 2
    recorder.on_click()
    assert recorder.pending_click is None


def test_the_very_first_click_is_a_single_click_even_at_the_clock_origin(recorder):
    # A monotonic clock's reference point is undefined, so a first click can land on any number, zero included.
    recorder.clock = 0.0
    recorder.on_click()
    run_pending(recorder)
    assert (recorder.opened_popup, recorder.opened_dashboard) == (1, 0)


class Stoppable:
    """Counts how many times it is asked to stop."""

    def __init__(self) -> None:
        """Start unstopped."""
        self.stops = 0

    def stop(self) -> None:
        """Count one more request to stop."""
        self.stops += 1


class Quitter:
    """A stand-in tray carrying only what quitting touches."""

    on_quit = tray.Tray.on_quit

    def __init__(self) -> None:
        """Start with nothing stopped."""
        self.stop_requested = threading.Event()
        self.pending_click: threading.Timer | None = None
        self.window = None
        self.notifier = Stoppable()
        self.icon = Stoppable()


def test_quitting_twice_stops_everything_once():
    # Quitting can be asked for twice at once: from the menu and from a Ctrl+C, or from an impatient second Ctrl+C.
    quitter = Quitter()
    quitter.on_quit()
    quitter.on_quit()
    assert (quitter.notifier.stops, quitter.icon.stops) == (1, 1)
