"""The tray icon: its menu, its hover text, the changes window it shows, and the poller that keeps them current."""

from __future__ import annotations

import subprocess
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import replace

from loguru import logger
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import APP_NAME
from .config import LOG_PATH, load_config
from .environment import autostart_enabled, hide_from_dock, on_console_interrupt, open_in_terminal, set_autostart
from .events import mark_seen
from .notifier import Notifier
from .popup import rows_to_show
from .service import PollResult, poll
from .settings_window import SettingsDialog
from .snapshot import read_snapshot
from .status import GREEN, GREY, Status, build_image, summary_line, tooltip_text
from .toolkit import FontZoom, application, follow_theme_setting, icon_from, layout_store
from .window import ChangesWindow

DEFAULT_DASHBOARD = "gh dash"
MENU_ENTRY_LIMIT = 10
TITLE_LIMIT = 50
# A failed poll is usually a transient GitHub error, so the next attempt comes sooner than a normal interval.
RETRY_FRACTION = 4
MINIMUM_WAIT_SECONDS = 60
# How often the toolkit's loop is nudged so that Python gets to run a signal handler. That is what lets Ctrl+C in
# the terminal that started the tray stop it on the platforms that have no console handler to offer.
HEARTBEAT_MS = 500


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


class Poller(QObject):
    """Polls GitHub on the configured interval, sooner when asked, and reports each result from a thread of its own.

    Polling runs on a background thread so the icon and its menu stay responsive while GitHub is slow. Each result
    is handed back over a signal, which the toolkit delivers on its own thread, where the icon and the window may
    be touched. Polls are serialised by there being one thread: two at once would run two collectors against the
    same baseline and announce the same change twice.
    """

    polled = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        """Prepare to poll, without starting."""
        super().__init__()
        self.stop_requested = threading.Event()
        self.asked = threading.Event()
        self.config = load_config()
        self.worker = threading.Thread(target=self.loop, daemon=True, name=f"{APP_NAME}-poll")

    def start(self) -> None:
        """Start polling."""
        self.worker.start()

    def ask(self) -> None:
        """Poll as soon as possible rather than at the end of the interval."""
        self.asked.set()

    def stop(self) -> None:
        """Stop after the poll under way, if any. The thread dies with the process, so nothing waits for it."""
        self.stop_requested.set()
        self.asked.set()

    def loop(self) -> None:
        """Poll on the configured interval until stopped."""
        while not self.stop_requested.is_set():
            succeeded = False
            try:
                self.config = load_config()
                logger.debug("polling")
                result = poll(self.config)
                succeeded = not result.error
                self.polled.emit(result)
            except Exception as error:  # a failed poll must not kill the timer
                logger.exception("poll failed unexpectedly")
                self.failed.emit(str(error)[:100])
            waiting = self.wait_seconds(succeeded)
            logger.debug("next poll in {} s unless asked sooner", waiting)
            self.wait_or_be_asked(waiting)

    def wait_seconds(self, succeeded: bool) -> int:
        """Return how long to wait before the next poll.

        :param succeeded: whether the poll that just ran worked
        """
        interval = self.config["poll_minutes"] * 60
        return max(MINIMUM_WAIT_SECONDS, interval if succeeded else interval // RETRY_FRACTION)

    def wait_or_be_asked(self, seconds: int) -> None:
        """Wait until the next poll is due, or until somebody asks for one sooner.

        :param seconds: how long to wait if nobody asks
        """
        if self.asked.wait(seconds):
            self.asked.clear()


class Tray(QObject):
    """The tray icon, its menu, the changes window, and the poller that keeps them current."""

    # Raised from whatever thread a console interrupt arrives on, so that quitting happens on the toolkit's thread.
    quit_asked = Signal()

    def __init__(self) -> None:
        """Load the settings, build the icon in its starting state, and build the changes window ready to show."""
        super().__init__()
        hide_from_dock()
        self.config = load_config()
        follow_theme_setting(self.config["theme"])
        self.status = Status()
        self.notifier = Notifier()
        self.stopping = False
        self.poller = Poller()
        self.poller.polled.connect(self.on_polled)
        self.poller.failed.connect(self.on_failed)
        self.icon = QSystemTrayIcon(icon_from(build_image(GREY, 0)), self)
        self.icon.setToolTip(f"{APP_NAME} - starting")
        # The menu is the application's own and is never handed to the desktop's tray. A desktop given a menu tends
        # to open it on every button, and the left click that should show the window never reaches the application.
        # Instead the menu is shown here on a right click, where the desktop reports one, and from the window.
        self.menu = QMenu()
        self.icon.activated.connect(self.on_activated)
        # The zoom is taken up before the window is built, so the window measures itself against the zoomed text.
        self.layout = layout_store()
        self.zoom = FontZoom(self.layout)
        self.window = ChangesWindow(rows_to_show(self.config["popup_rows"]), self.layout)
        self.window.refresh_asked.connect(self.on_refresh)
        self.window.dashboard_asked.connect(self.on_dashboard)
        self.window.attach_menu(self.menu)
        self.zoom.changed.connect(self.window.on_font_changed)
        self.settings: SettingsDialog | None = None
        self.heartbeat = QTimer(self)
        self.quit_asked.connect(self.on_quit)
        self.build_menu()

    def build_menu(self) -> None:
        """Rebuild the right-click menu against the current status and unread events."""
        self.menu.clear()
        # Clearing removes the entries but not a submenu, which stays a child of the menu, so a rebuild on every
        # poll would otherwise leave a review list behind each time.
        for stale in self.menu.findChildren(QMenu):
            stale.deleteLater()
        self.menu.addAction(summary_line(self.status)).setEnabled(False)
        self.menu.addAction("Open dashboard", self.on_dashboard)
        self.menu.addAction("Refresh now", self.on_refresh)
        self.menu.addSeparator()
        self.menu.addAction("Recent changes...", self.on_popup)
        self.menu.addMenu(self.reviews_menu())
        self.menu.addSeparator()
        self.menu.addAction("Mark all seen", self.on_mark_seen)
        login = self.menu.addAction("Start at login", self.on_toggle_autostart)
        login.setCheckable(True)
        login.setChecked(autostart_enabled())
        self.menu.addAction("Settings...", self.open_settings)
        self.menu.addAction("Open log", self.on_open_log)
        self.menu.addAction("Quit", self.on_quit)

    def reviews_menu(self) -> QMenu:
        """Return a submenu of the pull requests waiting on the user's review."""
        menu = QMenu("Awaiting your review", self.menu)
        stored, _damaged = read_snapshot()
        waiting = [entry for entry in (stored or {}).values() if entry.get("side") == "reviewing"]
        if not waiting:
            menu.addAction("nobody is waiting").setEnabled(False)
            return menu
        for entry in waiting[:MENU_ENTRY_LIMIT]:
            title = str(entry.get("title", ""))[:TITLE_LIMIT]
            menu.addAction(f"{entry['repo']}#{entry['number']} - {title}", self.opener(entry.get("url", "")))
        return menu

    def opener(self, url: str) -> Callable[..., None]:
        """Return a menu action that opens a page in the default browser.

        :param url: the page to open; a menu entry without one does nothing
        """

        def action(*_) -> None:
            if url:
                webbrowser.open(url)

        return action

    def repaint(self) -> None:
        """Push the current status into the icon, its hover text and its menu."""
        self.icon.setIcon(icon_from(build_image(self.status.colour, self.status.unread)))
        self.icon.setToolTip(tooltip_text(self.status, APP_NAME))
        self.build_menu()

    def on_polled(self, result: PollResult) -> None:
        """Show a poll's result, notify about what it found, and let the window know.

        :param result: what the poll found
        """
        self.config = self.poller.config
        self.status = result.status
        logger.debug(
            "poll result: {} unread, {} new event(s), icon {}, error {!r}",
            result.status.unread,
            len(result.events),
            result.status.colour,
            result.error,
        )
        self.repaint()
        # Notifying happens on a thread of its own. It talks to the desktop's notification service, which is outside
        # this application's control, and once wedged it would otherwise take the whole application with it.
        if result.events:
            threading.Thread(
                target=self.notifier.notify,
                args=(result.events, self.config["toasts"]),
                daemon=True,
                name=f"{APP_NAME}-notify-once",
            ).start()
        self.window.on_polled(not result.error)

    def on_failed(self, error: str) -> None:
        """Show that a poll failed in a way the poller did not expect.

        :param error: what went wrong, briefly
        """
        self.status = Status(colour=GREY, error=error)
        self.repaint()
        self.window.on_polled(False)

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Show or put away the changes window on a left click, and show the menu on a right or middle click.

        A middle click shows the menu because some desktops keep the right click to themselves and pass on only the
        middle one. A double click is the second of two left clicks, which has already been answered.

        :param reason: what was done to the icon
        """
        logger.debug("tray icon: {}", reason.name)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.on_popup()
        elif reason in (QSystemTrayIcon.ActivationReason.Context, QSystemTrayIcon.ActivationReason.MiddleClick):
            self.show_menu()

    def show_menu(self, *_) -> None:
        """Show the menu by the pointer."""
        spot = QCursor.pos()
        logger.debug("showing the menu at {},{}", spot.x(), spot.y())
        self.menu.popup(spot)

    def on_popup(self, *_) -> None:
        """Show the changes window by the pointer, or put it away if it is up."""
        spot = QCursor.pos()
        logger.debug("pointer at {},{}, window {}", spot.x(), spot.y(), "up" if self.window.isVisible() else "away")
        self.window.toggle(spot)

    def on_dashboard(self, *_) -> None:
        """Open the dashboard.

        Opening it says nothing about what the user has read, so nothing is marked seen. Rows are marked by
        clicking them in the changes window, and everything at once from this menu.
        """
        try:
            open_dashboard(self.config)
        except RuntimeError as error:
            logger.error("could not open the dashboard: {}", error)

    def on_refresh(self, *_) -> None:
        """Poll as soon as possible."""
        self.poller.ask()

    def on_mark_seen(self, *_) -> None:
        """Clear the unread count, and redraw the icon and the window."""
        mark_seen()
        self.status = replace(self.status, unread=0, colour=GREY if self.status.error else GREEN)
        self.repaint()
        self.window.reload()

    def on_toggle_autostart(self, *_) -> None:
        """Turn starting at login on or off."""
        set_autostart(not autostart_enabled())
        self.build_menu()

    def on_open_log(self, *_) -> None:
        """Open the log file in whatever the desktop opens text files with."""
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_PATH))):
            logger.error("the desktop refused to open the log at {}", LOG_PATH)

    def open_settings(self, *_) -> None:
        """Open the settings window, or bring it forward if it is already open."""
        if self.settings is None:
            self.settings = SettingsDialog()
            self.settings.accepted.connect(self.on_settings_saved)
            self.settings.finished.connect(self.on_settings_closed)
            self.zoom.changed.connect(self.settings.adjustSize)
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def on_settings_saved(self) -> None:
        """Take up the saved settings: the colours at once, and the rest on the next poll."""
        self.config = load_config()
        self.window.on_scheme_changed()

    def on_settings_closed(self, _result: int) -> None:
        """Forget the settings window once it is closed, so the next opening builds a fresh one."""
        self.settings = None

    def on_quit(self, *_) -> None:
        """Stop the poller, the notifier, the window and the icon, then the application.

        Idempotent, because quitting can be asked for twice at once: once from the menu and again from a Ctrl+C, or
        from an impatient second Ctrl+C while the first is still stopping things.
        """
        if self.stopping:
            return
        self.stopping = True
        self.poller.stop()
        self.notifier.stop()
        self.window.hide()
        self.icon.hide()
        application().quit()

    def run(self) -> None:
        """Show the icon, start polling, and run until quit."""
        # So Ctrl+C in the terminal that started the tray stops it, the same as the menu's Quit.
        on_console_interrupt(self.quit_asked.emit)
        self.heartbeat.timeout.connect(lambda: None)
        self.heartbeat.start(HEARTBEAT_MS)
        self.icon.show()
        self.poller.start()
        application().exec()
