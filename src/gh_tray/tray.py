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
from .settings_window import Account, AccountLookup, SettingsDialog
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
# Nudges the toolkit's loop so Ctrl+C can be handled where there is no console handler of its own.
HEARTBEAT_MS = 500


def open_dashboard(config: dict) -> None:
    """Open the terminal dashboard maximised, or run the command settings name, exactly as typed."""
    if config.get("dashboard_command"):
        # A user-typed command line needs a shell for its pipes, quoting and arguments to work.
        subprocess.Popen(config["dashboard_command"], shell=True)  # noqa: S602
        return
    open_in_terminal(DEFAULT_DASHBOARD, "gh-dash", maximised=True)


class Poller(QObject):
    """Polls GitHub on a thread of its own and reports each result over a signal.

    The signal is delivered on the toolkit's thread, where the icon and window may be touched. One thread polls,
    so two polls can never run at once and report the same change twice.
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
            except Exception as error:
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

    # Raised from whichever thread a console interrupt arrives on, so quitting happens on the toolkit's own thread.
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
        # Not given to the desktop tray: it opens on every click, swallowing left clicks; shown here on right-click.
        self.menu = QMenu()
        self.icon.activated.connect(self.on_activated)
        self.layout = layout_store()
        self.zoom = FontZoom(self.layout)
        self.window = ChangesWindow(rows_to_show(self.config["popup_rows"]), self.layout)
        self.window.refresh_asked.connect(self.on_refresh)
        self.window.dashboard_asked.connect(self.on_dashboard)
        self.window.attach_menu(self.menu)
        self.zoom.changed.connect(self.window.on_font_changed)
        self.settings: SettingsDialog | None = None
        self.account: Account | None = None
        self.lookup = AccountLookup()
        self.lookup.found.connect(self.on_account_found)
        self.heartbeat = QTimer(self)
        self.quit_asked.connect(self.on_quit)
        self.build_menu()

    def build_menu(self) -> None:
        """Rebuild the right-click menu against the current status and unread events."""
        self.menu.clear()
        # Clearing removes actions but not a submenu; without this, each rebuild leaves a stale review list.
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
        # Notifying runs on its own thread so a hang in the desktop's notification service cannot freeze the app.
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
        """Show/hide the window on left click; menu on right or middle click (some desktops route only middle-click).

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
        """Open the dashboard; marking seen happens elsewhere, by clicking a row or via "Mark all seen"."""
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
            self.settings = SettingsDialog(account=self.account)
            self.settings.accepted.connect(self.on_settings_saved)
            self.settings.finished.connect(self.on_settings_closed)
            self.zoom.changed.connect(self.settings.adjustSize)
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def on_settings_saved(self) -> None:
        """Apply the saved settings: colours change at once, everything else on the next poll."""
        self.config = load_config()
        self.window.on_scheme_changed()

    def on_settings_closed(self, _result: int) -> None:
        """Forget the settings window, then look up the account again to catch changes made since it opened."""
        self.settings = None
        self.account = None
        self.lookup.start()

    def on_account_found(self, account: Account) -> None:
        """Keep what GitHub said about the account for the next settings window.

        :param account: what GitHub said
        """
        self.account = account

    def on_quit(self, *_) -> None:
        """Stop everything, then the app; idempotent, since quitting can be asked twice at once (menu plus Ctrl+C)."""
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
        self.lookup.start()
        application().exec()
