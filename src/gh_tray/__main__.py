"""Command line entry point.

Running with no command shows the tray. ``once`` polls a single time and prints the result, which is the quickest
way to check the collector and the GitHub sign-in. ``settings`` opens the settings window on its own.
"""

from __future__ import annotations

import sys

import cyclopts
from loguru import logger

from . import APP_NAME, __version__
from .config import APP_DIR, LOCK_PATH, LOG_PATH, bootstrap
from .environment import SingleInstance
from .events import label_for
from .service import poll
from .status import tooltip_text

LOG_ROTATION = "1 MB"
LOG_RETENTION = 3

app = cyclopts.App(name=APP_NAME, version=__version__, help=__doc__)


def start_logging(to_console: bool) -> None:
    """Send diagnostics to a rotating file, and optionally to the console as well.

    :param to_console: whether to also log to standard error, which suits a foreground command
    """
    logger.remove()
    if to_console:
        logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} {level: <7} {message}")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(LOG_PATH, level="INFO", rotation=LOG_ROTATION, retention=LOG_RETENTION, encoding="utf-8")


@app.default
def run_tray() -> int:
    """Show the tray icon and poll on a timer.

    :return: process exit code
    """
    start_logging(to_console=False)
    lock = SingleInstance(LOCK_PATH)
    if not lock.acquire():
        logger.error("another instance is already running")
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 1
    try:
        from .tray import Tray

        logger.info("starting {} {}", APP_NAME, __version__)
        Tray().run()
    finally:
        lock.release()
    return 0


@app.command
def once() -> int:
    """Poll a single time, print the status and any changes, then exit.

    Refuses to run while the tray is up. Both would poll against the same stored comparison point, so whichever ran
    first would consume the changes and the other would never report them.

    :return: process exit code, non-zero when the poll failed or the tray is already running
    """
    start_logging(to_console=True)
    lock = SingleInstance(LOCK_PATH)
    if not lock.acquire():
        print(f"{APP_NAME} is already running. Use its Refresh now menu entry, or quit it first.", file=sys.stderr)
        return 1
    try:
        return report_one_poll()
    finally:
        lock.release()


def report_one_poll() -> int:
    """Poll once and print what it found.

    :return: process exit code, non-zero when the poll failed
    """
    result = poll(bootstrap())
    if result.error:
        print(f"poll failed: {result.error}", file=sys.stderr)
        return 1
    print(tooltip_text(result.status, APP_NAME))
    if result.first_run:
        print("\nbaseline established, so there is nothing to compare against yet")
    elif result.events:
        print(f"\n{len(result.events)} change(s):")
        for event in result.events:
            print(f"  {label_for(event['kind']):<20} {event['key']:<45} {event['detail']}")
    else:
        print("\nno changes since the previous poll")
    return 0


@app.command
def settings() -> int:
    """Open the settings window.

    :return: process exit code
    """
    start_logging(to_console=True)
    from .settings_window import run_settings

    run_settings()
    return 0


@app.command
def popup() -> int:
    """Show the most recent changes in a small frameless window.

    :return: process exit code
    """
    start_logging(to_console=False)
    from .popup import show_popup

    show_popup()
    return 0


def main() -> None:
    """Run the command line application."""
    sys.exit(app() or 0)


if __name__ == "__main__":
    main()
