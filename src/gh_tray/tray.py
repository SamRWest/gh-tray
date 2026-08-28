"""The tray icon: its menu, its hover text, and the timer that keeps them current."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace

import pystray
from loguru import logger

from . import APP_MODULE, APP_NAME
from .config import POPUP_REQUEST_PATH, REFRESH_REQUEST_PATH, load_config
from .environment import autostart_enabled, cursor_position, make_dpi_aware, no_console_flag, open_in_terminal, set_autostart
from .events import mark_seen
from .notifier import Notifier
from .popup import request_popup, start_window, window_waiting
from .service import poll
from .snapshot import read_snapshot
from .status import GREEN, GREY, Status, build_image, summary_line, tooltip_text

DEFAULT_DASHBOARD = "gh dash"
MENU_ENTRY_LIMIT = 10
TITLE_LIMIT = 50
# A failed poll is usually a transient GitHub error, so the next attempt comes sooner than a normal interval.
RETRY_FRACTION = 4
MINIMUM_WAIT_SECONDS = 60
# How often to look for a window asking for a poll while waiting for the next one.
REQUEST_CHECK_SECONDS = 3
# How close together two clicks on the icon must be to count as one double click. The tray library reports every
# left click and offers no double click of its own, so the pair is recognised here.
DOUBLE_CLICK_SECONDS = 0.5


def open_dashboard(config: dict) -> None:
    """Open the terminal dashboard maximised, or whichever command the settings name instead.

    A command named in the settings is run as given, since how its own window opens is then the user's business.
    """
    if config.get("dashboard_command"):
        # Through a shell on purpose: this is a command line the user typed into their own settings, and running it
        # any other way would refuse the pipes, quoting and arguments that make it worth setting at all.
        subprocess.Popen(config["dashboard_command"], shell=True)  # noqa: S602
        return
    open_in_terminal(DEFAULT_DASHBOARD, "gh-dash", maximised=True)


def open_settings() -> None:
    """Open the settings window as its own process, keeping its event loop clear of the tray's."""
    subprocess.Popen([sys.executable, "-m", APP_MODULE, "settings"], creationflags=no_console_flag())


class Tray:
    """The tray icon, its menu, and the background thread that polls on a timer."""

    def __init__(self) -> None:
        """Load the settings and build the icon in its starting state."""
        # The tray measures the screen when it records where a click was. An unaware process is lied to about
        # coordinates on a scaled display, and the window, which is aware, then opens where the lie says.
        make_dpi_aware()
        self.config = load_config()
        REFRESH_REQUEST_PATH.unlink(missing_ok=True)
        POPUP_REQUEST_PATH.unlink(missing_ok=True)
        self.status = Status()
        # The process holding the changes window, started on demand and stopped when the tray quits.
        self.window: subprocess.Popen | None = None
        self.notifier = Notifier()
        self.stop_requested = threading.Event()
        self.poll_lock = threading.Lock()
        # No sentinel number: the reference point of a monotonic clock is undefined, so any number chosen to mean
        # "no click yet" could legitimately occur and would turn the very first click into a double click.
        self.last_click: float | None = None
        self.pending_click: threading.Timer | None = None
        self.icon = pystray.Icon(APP_NAME, build_image(GREY, 0), f"{APP_NAME} - starting", menu=self.build_menu())

    def build_menu(self) -> pystray.Menu:
        """Rebuild the right-click menu against the current status and unread events."""
        item, menu = pystray.MenuItem, pystray.Menu
        return menu(
            # An invisible entry carries the default action, which is what a click on the icon triggers. Keeping it
            # separate from the visible entry lets a click be paired into a double click while the menu entry still
            # acts at once.
            item("", self.on_click, default=True, visible=False),
            item(summary_line(self.status), None, enabled=False),
            item("Open dashboard", self.on_dashboard),
            item("Refresh now", self.on_refresh),
            menu.SEPARATOR,
            item("Recent changes...", self.on_popup),
            item("Awaiting your review", self.reviews_menu()),
            menu.SEPARATOR,
            item("Mark all seen", self.on_mark_seen),
            item("Start at login", self.on_toggle_autostart, checked=lambda _item: autostart_enabled()),
            item("Settings...", lambda _icon=None, _item=None: open_settings()),
            item("Quit", self.on_quit),
        )

    def reviews_menu(self) -> pystray.Menu:
        """Return a submenu of the pull requests waiting on the user's review."""
        item, menu = pystray.MenuItem, pystray.Menu
        stored, _damaged = read_snapshot()
        waiting = [entry for entry in (stored or {}).values() if entry.get("side") == "reviewing"]
        if not waiting:
            return menu(item("nobody is waiting", None, enabled=False))
        return menu(
            *(
                item(f"{entry['repo']}#{entry['number']} - {str(entry.get('title', ''))[:TITLE_LIMIT]}", self.opener(entry.get("url", "")))
                for entry in waiting[:MENU_ENTRY_LIMIT]
            )
        )

    def opener(self, url: str) -> Callable[..., None]:
        """Return a menu action that opens a page in the default browser.

        :param url: the page to open; a menu entry without one does nothing
        """

        def action(_icon=None, _item=None) -> None:
            if url:
                webbrowser.open(url)

        return action

    def repaint(self) -> None:
        """Push the current status into the icon, its hover text and its menu."""
        self.icon.icon = build_image(self.status.colour, self.status.unread)
        self.icon.title = tooltip_text(self.status, APP_NAME)
        self.icon.menu = self.build_menu()
        self.icon.update_menu()

    def refresh(self) -> bool:
        """Poll once and show the result.

        Polls are serialised. Two at once would run two collectors against the same baseline file, and both could
        read the same previous snapshot and so record and announce the same change twice.

        :return: whether the poll succeeded
        """
        with self.poll_lock:
            self.config = load_config()
            result = poll(self.config)
            self.status = result.status
            self.repaint()
        # Notifying happens after the lock is dropped and on a thread of its own. It talks to the desktop's
        # notification service, which is outside this application's control, and once wedged it took the poll lock
        # with it and the whole application stopped responding.
        if result.events:
            threading.Thread(
                target=self.notifier.notify,
                args=(result.events, self.config["toasts"]),
                daemon=True,
                name=f"{APP_NAME}-notify-once",
            ).start()
        return not result.error

    def wait_seconds(self, succeeded: bool) -> int:
        """Return how long to wait before the next poll.

        :param succeeded: whether the poll that just ran worked
        """
        interval = self.config["poll_minutes"] * 60
        return max(MINIMUM_WAIT_SECONDS, interval if succeeded else interval // RETRY_FRACTION)

    def loop(self) -> None:
        """Poll on the configured interval until the icon quits."""
        while not self.stop_requested.is_set():
            succeeded = False
            try:
                succeeded = self.refresh()
            except Exception as error:  # a failed poll must not kill the timer
                logger.exception("poll failed unexpectedly")
                self.status = Status(colour=GREY, error=str(error)[:100])
                self.repaint()
            self.wait_or_be_asked(self.wait_seconds(succeeded))

    def wait_or_be_asked(self, seconds: int) -> None:
        """Wait until the next poll is due, or until a window asks for one sooner.

        The waiting is broken into short spells so a request left by another process is noticed within a few
        seconds rather than at the end of the interval, which is the difference between a Refresh button that
        works and one that appears to do nothing.

        :param seconds: how long to wait if nobody asks
        """
        for _spell in range(max(1, seconds // REQUEST_CHECK_SECONDS)):
            if self.stop_requested.wait(REQUEST_CHECK_SECONDS):
                return
            if REFRESH_REQUEST_PATH.exists():
                REFRESH_REQUEST_PATH.unlink(missing_ok=True)
                logger.info("a window asked for a fresh look")
                return

    def on_click(self, *_) -> None:
        """Handle a click on the icon: one click shows the recent changes, two open the dashboard.

        A single click cannot act at once, because the first click of a double click looks exactly like it. So it is
        held for as long as a double click may take, and cancelled if a second click arrives.
        """
        now = time.monotonic()
        if self.pending_click is not None:
            self.pending_click.cancel()
            self.pending_click = None
        if self.last_click is not None and now - self.last_click <= DOUBLE_CLICK_SECONDS:
            self.last_click = None
            self.on_dashboard()
            return
        self.last_click = now
        self.pending_click = threading.Timer(DOUBLE_CLICK_SECONDS, self.on_popup)
        self.pending_click.daemon = True
        self.pending_click.start()

    def on_popup(self, *_) -> None:
        """Ask the waiting window to show the recent changes, starting one if none is waiting.

        The window is a separate process that stays loaded and hidden, so this is a note rather than a launch. One
        note serves any number of clicks, which is what stops several impatient ones opening several windows.
        """
        request_popup(cursor_position())
        if not window_waiting():
            self.window = start_window()

    def on_dashboard(self, *_) -> None:
        """Open the dashboard.

        Opening it says nothing about what the user has read, so nothing is marked seen. Rows are marked by
        clicking them in the recent changes window, and everything at once from this menu.
        """
        try:
            open_dashboard(self.config)
        except RuntimeError as error:
            logger.error("could not open the dashboard: {}", error)

    def on_refresh(self, *_) -> None:
        """Poll immediately, off the thread handling the menu, unless a poll is already under way."""
        if self.poll_lock.locked():
            logger.info("a poll is already running, ignoring the refresh")
            return
        threading.Thread(target=self.refresh, daemon=True, name=f"{APP_NAME}-refresh").start()

    def on_mark_seen(self, *_) -> None:
        """Clear the unread count and repaint."""
        mark_seen()
        self.status = replace(self.status, unread=0, colour=GREY if self.status.error else GREEN)
        self.repaint()

    def on_toggle_autostart(self, *_) -> None:
        """Turn starting at login on or off."""
        set_autostart(not autostart_enabled())
        self.icon.update_menu()

    def on_quit(self, *_) -> None:
        """Stop the timers, the notifier, the waiting window and the icon."""
        self.stop_requested.set()
        if self.pending_click is not None:
            self.pending_click.cancel()
            self.pending_click = None
        if self.window is not None and self.window.poll() is None:
            # It has no icon of its own, so left running it would be a process nobody could see or stop.
            self.window.terminate()
        self.notifier.stop()
        self.icon.stop()

    def run(self) -> None:
        """Show the icon, start polling, and load the changes window ready for the first click."""
        threading.Thread(target=self.loop, daemon=True, name=f"{APP_NAME}-poll").start()
        if not window_waiting():
            self.window = start_window()
        self.icon.run()
