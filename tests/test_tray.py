"""The tray icon's menu, its poller, and how a click or a poll result reaches the changes window."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QSystemTrayIcon

from gh_tray import config, tray, window
from gh_tray.service import PollResult
from gh_tray.status import GREEN, GREY, RED, Status, summary_line
from gh_tray.tray import Tray


@pytest.fixture
def build_tray(qtbot, monkeypatch):
    """Return a function that builds a real Tray with every outside file access stubbed."""
    monkeypatch.setattr(tray, "load_config", lambda: copy.deepcopy(config.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "rows_to_show", lambda _count: [])
    monkeypatch.setattr(window, "rows_to_show", lambda _count: [])
    monkeypatch.setattr(window, "load_config", lambda: {"popup_rows": 20})
    monkeypatch.setattr(tray, "read_snapshot", lambda: ({}, False))
    monkeypatch.setattr(tray, "autostart_enabled", lambda: False)
    monkeypatch.setattr(tray, "mark_seen", lambda: None)

    def build() -> Tray:
        subject = Tray()
        qtbot.addWidget(subject.window)
        return subject

    return build


class Stoppable:
    """Counts how many times it is asked to stop, standing in for the poller or the notifier."""

    def __init__(self) -> None:
        """Start unstopped."""
        self.stops = 0

    def stop(self) -> None:
        """Count one more request to stop."""
        self.stops += 1


def test_trigger_toggles_the_window_and_double_click_does_not(build_tray, qapp):
    subject = build_tray()
    assert not subject.window.isVisible()
    subject.on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    qapp.processEvents()
    assert subject.window.isVisible()
    subject.on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
    qapp.processEvents()
    assert subject.window.isVisible(), "a double click must leave the window exactly as the trigger left it"


def test_on_quit_twice_stops_the_poller_and_notifier_once(build_tray):
    # Quitting can be asked for twice at once: from the menu and from a Ctrl+C, or from an impatient second Ctrl+C.
    subject = build_tray()
    subject.poller = Stoppable()
    subject.notifier = Stoppable()
    subject.on_quit()
    subject.on_quit()
    assert (subject.poller.stops, subject.notifier.stops) == (1, 1)


def test_on_polled_with_an_error_paints_a_grey_status_and_tells_the_window(build_tray, monkeypatch):
    subject = build_tray()
    told = []
    monkeypatch.setattr(subject.window, "on_polled", told.append)
    result = PollResult(status=Status(colour=GREY, error="collector timed out"), error="collector timed out")
    subject.on_polled(result)
    assert subject.status.colour == GREY
    assert told == [False]


def test_on_mark_seen_zeroes_unread_and_turns_a_green_status(build_tray):
    subject = build_tray()
    subject.status = replace(subject.status, unread=5, colour=RED)
    subject.on_mark_seen()
    assert (subject.status.unread, subject.status.colour) == (0, GREEN)


def test_the_menu_lists_the_expected_entries_in_order(build_tray):
    subject = build_tray()
    labels = [action.text() for action in subject.menu.actions() if not action.isSeparator()]
    assert labels == [
        summary_line(subject.status),
        "Open dashboard",
        "Refresh now",
        "Recent changes...",
        "Awaiting your review",
        "Mark all seen",
        "Start at login",
        "Settings...",
        "Quit",
    ]


def test_wait_or_be_asked_returns_early_when_asked_from_another_thread_and_clears_the_flag(monkeypatch):
    monkeypatch.setattr(tray, "load_config", lambda: copy.deepcopy(config.DEFAULT_CONFIG))
    poller = tray.Poller()
    asker = threading.Timer(0.05, poller.ask)
    asker.start()
    started = time.monotonic()
    poller.wait_or_be_asked(5)
    elapsed = time.monotonic() - started
    asker.join()
    assert elapsed < 1, "asking from another thread should cut the wait short rather than running the full interval"
    assert not poller.asked.is_set()


def test_wait_seconds_keeps_the_minimum_and_quarters_the_interval_after_a_failure(monkeypatch):
    monkeypatch.setattr(tray, "load_config", lambda: copy.deepcopy(config.DEFAULT_CONFIG))
    poller = tray.Poller()
    poller.config = {"poll_minutes": 0}
    assert poller.wait_seconds(succeeded=True) == tray.MINIMUM_WAIT_SECONDS
    poller.config = {"poll_minutes": 20}
    assert poller.wait_seconds(succeeded=True) == 1200
    assert poller.wait_seconds(succeeded=False) == 1200 // tray.RETRY_FRACTION


def test_a_middle_click_shows_the_menu_for_a_desktop_that_keeps_the_right_click(build_tray, qtbot):
    subject = build_tray()
    subject.on_activated(QSystemTrayIcon.ActivationReason.MiddleClick)
    qtbot.waitUntil(subject.menu.isVisible)
    assert not subject.window.isVisible()
    subject.menu.close()


def test_a_right_click_shows_the_applications_own_menu(build_tray, qtbot):
    # The desktop is never given the menu, so a right click it reports is answered here.
    subject = build_tray()
    assert subject.icon.contextMenu() is None
    subject.on_activated(QSystemTrayIcon.ActivationReason.Context)
    qtbot.waitUntil(subject.menu.isVisible)
    subject.menu.close()


def test_the_window_carries_the_same_menu(build_tray):
    subject = build_tray()
    assert subject.window.menu_button.menu() is subject.menu
    assert subject.window.menu_button.isVisibleTo(subject.window)
