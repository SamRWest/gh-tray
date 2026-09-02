"""Desktop notifications for detected changes.

Clicking a notification opens the pull request it is about in the default browser.

Notifications are raised from a long-lived event loop on its own thread. The platform backend calls back into the
sending loop when a notification is clicked or dismissed, which can happen long after the send returns, so a loop
that is closed straight after sending would raise on that callback and no click could ever be handled.
"""

from __future__ import annotations

import asyncio
import threading
import webbrowser

from desktop_notifier import DesktopNotifier, Icon
from desktop_notifier.backends.dummy import DummyNotificationCenter
from desktop_notifier.main import get_backend_class
from loguru import logger

from . import APP_NAME
from .config import APP_ICON_PATH
from .environment import notify_by_script
from .events import label_for
from .status import write_app_icon

MAX_LINES_PER_NOTIFICATION = 4
SEND_TIMEOUT_SECONDS = 30


def own_icon() -> Icon | None:
    """Return the application's own mark for a notification to carry, or nothing if it cannot be drawn.

    Without one the notification service falls back to the icon of whatever program raised it, which for a Python
    application is the Python logo: nothing to do with this application and no help in telling it apart.

    :return: the icon, or None where drawing it failed
    """
    try:
        return Icon(path=write_app_icon(APP_ICON_PATH))
    except OSError as error:
        logger.warning("could not draw the application's icon: {}", error)
        return None


def notification_center_available() -> bool:
    """Return whether the desktop's notification service will take notifications from this process.

    macOS hands Notification Center only to an app bundle, and a Python interpreter installed by a package manager
    is not one, so the notification library quietly substitutes a backend that does nothing. Which backend it chose
    is asked of it rather than worked out again here.
    """
    return get_backend_class() is not DummyNotificationCenter


class Notifier:
    """Raises one desktop notification per poll, covering every change the user has asked to be told about."""

    def __init__(self, app_name: str = APP_NAME) -> None:
        """:param app_name: name the platform shows as the notification's source."""
        self.app_name = app_name
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._backend: DesktopNotifier | None = None
        self._stopped = False

    def _ready(self) -> tuple[asyncio.AbstractEventLoop | None, DesktopNotifier | None]:
        """Return the running loop and backend, starting them on first use.

        Nothing slow or unpredictable happens while the lock is held: the icon is drawn first, and everything this
        needs is imported when the module is. Doing either inside the lock, on the thread that also holds the poll
        lock, is what wedged the whole application once.

        Both values are read inside the lock and returned as locals, so a concurrent stop cannot leave the caller
        holding a half-torn-down pair. Once stopped, nothing is started again: a notification after shutdown would
        otherwise raise a fresh loop and thread that nobody would ever stop.

        :return: the event loop and the desktop notifier bound to it, or a pair of None once stopped
        """
        icon = own_icon()
        with self._lock:
            if self._stopped:
                return None, None
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, daemon=True, name=f"{self.app_name}-notify").start()
                self._backend = DesktopNotifier(app_name=self.app_name, app_icon=icon)
            return self._loop, self._backend

    def wanted(self, events: list[dict], toast_settings: dict) -> list[dict]:
        """Return the events the user has enabled a notification for.

        :param events: every change detected this poll
        :param toast_settings: the per-kind switches from the settings
        """
        return [event for event in events if toast_settings.get(event["kind"])]

    def compose(self, events: list[dict]) -> tuple[str, str]:
        """Return the title and body for one notification covering several changes.

        :param events: the changes to describe, already filtered to those the user wants
        :return: title and body text
        """
        shown = events[:MAX_LINES_PER_NOTIFICATION]
        body = "\n".join(f"{label_for(event['kind'])}: {event['key']}" for event in shown)
        if len(events) > len(shown):
            body += f"\n+{len(events) - len(shown)} more"
        return f"{len(events)} GitHub change{'s' if len(events) != 1 else ''}", body

    def target_url(self, events: list[dict]) -> str:
        """Return the page a click on the notification should open.

        The notification lists changes in the order they were detected, so the first one carrying a page is the one
        the reader sees at the top and the one a click most plausibly means.

        :param events: the changes the notification describes
        :return: the address to open, empty when none of them carries one
        """
        return next((event["url"] for event in events if event.get("url")), "")

    def notify(self, events: list[dict], toast_settings: dict) -> bool:
        """Raise one notification covering every enabled change.

        :param events: every change detected this poll
        :param toast_settings: the per-kind switches from the settings
        :return: whether a notification was raised
        """
        chosen = self.wanted(events, toast_settings)
        if not chosen:
            return False
        title, body = self.compose(chosen)
        url = self.target_url(chosen)

        def clicked() -> None:
            """Open the pull request the notification is about, in the default browser."""
            if url:
                webbrowser.open(url)

        if not notification_center_available():
            # Spoken rather than raised: the desktop will not take a notification from this process, so the
            # scripting bridge carries the words alone, with no icon and nothing to click.
            notify_by_script(title, body)
            logger.info(
                "notified about {} change(s) by script, as this desktop offers this process no notification service",
                len(chosen),
            )
            return True
        logger.debug("raising a notification about {} change(s)", len(chosen))
        loop, backend = self._ready()
        if loop is None or backend is None:
            logger.debug("not notifying, the notifier has been stopped")
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                backend.send(title=title, message=body, on_clicked=clicked),
                loop,
            ).result(timeout=SEND_TIMEOUT_SECONDS)
        except Exception as error:  # a notification failing must never stop the poll loop
            logger.error("could not raise a notification: {}", error)
            return False
        logger.info("notified about {} change(s)", len(chosen))
        return True

    def stop(self) -> None:
        """Stop the notification loop, if one was started, and refuse to start another."""
        with self._lock:
            self._stopped = True
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._loop = None
                self._backend = None
