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
from .config import bootstrap, load_config
from .environment import autostart_enabled, hidden_window_flags, open_in_terminal, set_autostart
from .events import label_for, mark_seen, recent_events, unread_events
from .notifier import Notifier
from .service import poll, read_snapshot
from .status import GREEN, GREY, Status, build_image, summary_line, tooltip_text

DEFAULT_DASHBOARD = "gh dash"
MENU_ENTRY_LIMIT = 10
TITLE_LIMIT = 50
# A failed poll is usually a transient GitHub error, so the next attempt comes sooner than a normal interval.
RETRY_FRACTION = 4
MINIMUM_WAIT_SECONDS = 60
# How close together two clicks on the icon must be to count as one double click. The tray library reports every
# left click and offers no double click of its own, so the pair is recognised here.
DOUBLE_CLICK_SECONDS = 0.5


def open_dashboard(config: dict) -> None:
    """Open the terminal dashboard, or whichever command the settings name instead."""
    command = config.get("dashboard_command") or DEFAULT_DASHBOARD
    if config.get("dashboard_command"):
        subprocess.Popen(command, shell=True)
        return
    open_in_terminal(command, "gh-dash")


def open_settings() -> None:
    """Open the settings window as its own process, keeping its event loop clear of the tray's."""
    subprocess.Popen([sys.executable, "-m", "gh_tray", "settings"], **hidden_window_flags())


class Tray:
    """The tray icon, its menu, and the background thread that polls on a timer."""

    def __init__(self) -> None:
        """Load the settings and build the icon in its starting state."""
        self.config = bootstrap()
        self.status = Status()
        self.notifier = Notifier()
        self.stop_requested = threading.Event()
        self.poll_lock = threading.Lock()
        self.last_click = 0.0
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
            item("Recent changes", self.changes_menu()),
            item("Awaiting your review", self.reviews_menu()),
            menu.SEPARATOR,
            item("Mark all seen", self.on_mark_seen),
            item("Start at login", self.on_toggle_autostart, checked=lambda _item: autostart_enabled()),
            item("Settings...", lambda _icon=None, _item=None: open_settings()),
            item("Quit", self.on_quit),
        )

    def changes_menu(self) -> pystray.Menu:
        """Return a submenu of unread changes, falling back to recent ones, each opening its page in a browser."""
        item, menu = pystray.MenuItem, pystray.Menu
        events = unread_events() or recent_events(MENU_ENTRY_LIMIT)
        if not events:
            return menu(item("nothing new", None, enabled=False))
        entries = [item(f"{label_for(event['kind'])} - {event['key']}", self.opener(event.get("url", ""))) for event in events[:MENU_ENTRY_LIMIT]]
        return menu(*entries)

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
            self.stop_requested.wait(self.wait_seconds(succeeded))

    def on_click(self, *_) -> None:
        """Handle a click on the icon, opening the dashboard when it completes a double click."""
        now = time.monotonic()
        if now - self.last_click <= DOUBLE_CLICK_SECONDS:
            self.last_click = 0.0
            self.on_dashboard()
            return
        self.last_click = now

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
        """Stop the timer, the notifier and the icon."""
        self.stop_requested.set()
        self.notifier.stop()
        self.icon.stop()

    def run(self) -> None:
        """Show the icon and start polling."""
        threading.Thread(target=self.loop, daemon=True, name=f"{APP_NAME}-poll").start()
        self.icon.run()
