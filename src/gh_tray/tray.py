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

from . import APP_NAME
from .config import REFRESH_REQUEST_PATH, load_config
from .environment import autostart_enabled, hidden_window_flags, open_in_terminal, set_autostart
from .events import mark_seen
from .notifier import Notifier
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
        subprocess.Popen(config["dashboard_command"], shell=True)
        return
    open_in_terminal(DEFAULT_DASHBOARD, "gh-dash", maximised=True)


def open_settings() -> None:
    """Open the settings window as its own process, keeping its event loop clear of the tray's."""
    subprocess.Popen([sys.executable, "-m", "gh_tray", "settings"], **hidden_window_flags())


class Tray:
    """The tray icon, its menu, and the background thread that polls on a timer."""

    def __init__(self) -> None:
        """Load the settings and build the icon in its starting state."""
        self.config = load_config()
        REFRESH_REQUEST_PATH.unlink(missing_ok=True)
        self.status = Status()
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
        entries, _damaged = read_snapshot()
        waiting = [entry for entry in (entries or {}).values() if entry.get("side") == "reviewing"]
        if not waiting:
            return menu(item("nobody is waiting", None, enabled=False))
        entries = [
            item(f"{entry['repo']}#{entry['number']} - {str(entry.get('title', ''))[:TITLE_LIMIT]}", self.opener(entry.get("url", "")))
            for entry in waiting[:MENU_ENTRY_LIMIT]
        ]
        return menu(*entries)

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
            if result.events:
                self.notifier.notify(result.events, self.config["toasts"])
            self.repaint()
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
        """Show the recent changes in a small window beside the tray."""
        subprocess.Popen([sys.executable, "-m", "gh_tray", "popup"], **hidden_window_flags())

    def on_dashboard(self, *_) -> None:
        """Open the dashboard and treat that as the user having looked."""
        try:
            open_dashboard(self.config)
        except RuntimeError as error:
            logger.error("could not open the dashboard: {}", error)
            return
        self.on_mark_seen()

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
        """Stop the timers, the notifier and the icon."""
        self.stop_requested.set()
        if self.pending_click is not None:
            self.pending_click.cancel()
            self.pending_click = None
        self.notifier.stop()
        self.icon.stop()

    def run(self) -> None:
        """Show the icon and start polling."""
        threading.Thread(target=self.loop, daemon=True, name=f"{APP_NAME}-poll").start()
        self.icon.run()
