"""Command line entry point.

Running with no command shows the tray. ``once`` polls a single time and prints the result, which is the quickest
way to check the collector and the GitHub sign-in. ``settings`` opens the settings window on its own.
"""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from types import TracebackType

import cyclopts
from loguru import logger

from . import APP_NAME, __version__
from .config import APP_DIR, LOCK_PATH, LOG_PATH, STDERR_PATH, load_config
from .environment import SingleInstance, launch_command, start_detached
from .events import label_for
from .service import poll
from .status import tooltip_text

LOG_ROTATION = "1 MB"
LOG_RETENTION = 3
LIGHTS = ("🟢", "🟡", "🔴")
PLAIN_LIGHTS = ("[ ok ]", "[ -- ]", "[ !! ]")
app = cyclopts.App(name=APP_NAME, version=__version__, help=__doc__)


def start_logging(to_console: bool, verbose: bool = False) -> None:
    """Send diagnostics to a rotating file, and optionally to the console as well.

    The file takes everything down to the debug level, which is what a report from another desktop needs, and the
    rotation keeps that from ever amounting to much. The console takes the debug level only when asked.

    :param to_console: whether to also log to standard error, which suits a foreground command
    :param verbose: whether the console should carry the debug level too
    """
    logger.remove()
    if to_console:
        level = "DEBUG" if verbose else "INFO"
        logger.add(sys.stderr, level=level, format="{time:HH:mm:ss} {level: <7} {message}")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(LOG_PATH, level="DEBUG", rotation=LOG_ROTATION, retention=LOG_RETENTION, encoding="utf-8")


class LinesToLog(io.TextIOBase):
    """A stand-in for the standard error stream that hands complete lines to the log.

    Anything the tray would have printed to a file nobody reads lands in the one log instead: a stray print, the
    warnings module, whatever a library writes. A write made while a line is already being logged goes to the real
    stream instead, because that is the log reporting trouble of its own, and logging it would go round forever.
    """

    def __init__(self) -> None:
        """Start with nothing buffered."""
        self.tail = ""
        self.forwarding = threading.local()

    def write(self, text: str) -> int:
        """Take one write, and log each complete line it finishes.

        :param text: whatever was written, which need not end a line
        :return: how much was taken, which is all of it
        """
        if getattr(self.forwarding, "busy", False):
            if sys.__stderr__ is not None:
                sys.__stderr__.write(text)
            return len(text)
        self.forwarding.busy = True
        try:
            *lines, self.tail = (self.tail + text).split("\n")
            for line in lines:
                if line.strip():
                    logger.warning("stderr: {}", line)
        finally:
            self.forwarding.busy = False
        return len(text)


def log_uncaught(kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
    """Record an exception nothing caught, since nobody is watching a tray's console.

    :param kind: the exception's class
    :param error: the exception itself
    :param trace: where it happened
    """
    logger.opt(exception=(kind, error, trace)).error("uncaught in the main thread")


def log_uncaught_in_thread(args: threading.ExceptHookArgs) -> None:
    """Record an exception that escaped a thread, which would otherwise die saying nothing.

    :param args: what the thread machinery reports about the escape
    """
    where = args.thread.name if args.thread else "an unnamed thread"
    logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).error("uncaught in {}", where)


def capture_stray_output() -> None:
    """Send everything the tray would have written to standard error to the log instead.

    The tray runs in the foreground of no terminal, so anything printed is otherwise read by nobody. Only writes
    from native code, which never pass through Python, still land in the standard error file, which is what it is
    kept for.
    """
    sys.excepthook = log_uncaught
    threading.excepthook = log_uncaught_in_thread
    sys.stderr = LinesToLog()


def linked(path: Path) -> str:
    """Return a file's name as a terminal hyperlink to it, or its whole path where links cannot be drawn.

    :param path: the file to name
    """
    if sys.stdout is None or not sys.stdout.isatty():
        return str(path)
    return f"\x1b]8;;{path.as_uri()}\x1b\\{path.name}\x1b]8;;\x1b\\"


def lights() -> tuple[str, str, str]:
    """Return the marks for present, installable and needs-you, in a form this console can actually print.

    A Windows console still running a legacy codepage cannot encode a coloured circle and raises rather than
    substituting, so plain words stand in where that is the case.
    """
    try:
        for light in LIGHTS:
            light.encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return PLAIN_LIGHTS
    return LIGHTS


def print_status() -> list:
    """Print every outside tool with a light saying whether it is here, and return the ones that are not.

    Green is present, amber is missing but can be installed from here, red is missing and needs the user to act.

    :return: the requirements that are not satisfied
    """
    from .prerequisites import requirements

    present_mark, installable_mark, manual_mark = lights()
    outstanding = []
    listed = requirements()
    # Padded to the longest name, so the notes line up whatever the platform adds to the list.
    width = max(len(requirement.name) for requirement, _present in listed)
    print(f"{APP_NAME} needs these:\n")
    for requirement, present in listed:
        light = present_mark if present else (installable_mark if requirement.installable else manual_mark)
        note = requirement.summary if present else (" ".join(requirement.command) or requirement.manual)
        print(f"  {light}  {requirement.name:<{width}}  {note}")
        if not present:
            outstanding.append(requirement)
    print()
    return outstanding


def offer_to_install(assume_yes: bool = False) -> bool:
    """Report anything missing and offer to install what can be installed.

    :param assume_yes: install without asking, for a caller that has already decided
    :return: whether everything is now present
    """
    from .prerequisites import install, missing

    outstanding = print_status()
    if not outstanding:
        print("Nothing to do.")
        return True
    installable = [requirement for requirement in outstanding if requirement.installable]
    if not installable:
        print("None of these can be installed from here. Run the commands above, then try again.")
        return False
    if not assume_yes and input(f"Install {len(installable)} of these now? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing installed.")
        return False
    for requirement in installable:
        print(f"\nInstalling {requirement.name}...")
        install(requirement)
    print()
    still_missing = missing()
    for requirement in still_missing:
        print(f"{lights()[2]}  Still missing: {requirement.name}. {requirement.manual or 'Install it and try again.'}")
    return not still_missing


@app.command
def setup(yes: bool = False) -> int:
    """Check for the outside tools gh-tray needs, and offer to install any that are missing.

    :param yes: install without asking first
    :return: process exit code, non-zero when something is still missing
    """
    start_logging(to_console=True)
    return 0 if offer_to_install(assume_yes=yes) else 1


@app.default
def run_tray(foreground: bool = False, verbose: bool = False) -> int:
    """Start the tray, which then runs on its own, and return.

    The tray outlives the terminal it was started from and prints nothing there, so this says that it started and
    where it writes, and comes straight back. With ``--foreground`` the tray runs in this process instead, attached
    to the terminal, where Ctrl+C stops it and its log is written to the console as well.

    :param foreground: run the tray here rather than as a process of its own
    :param verbose: write the debug level to the console as well, which the log file always carries
    :return: process exit code
    """
    # The console is written to only when somebody can read it. A tray started on its own, or by a login entry, runs
    # in the foreground of no terminal, and its log has a file of its own.
    start_logging(to_console=foreground and sys.stderr is not None and sys.stderr.isatty(), verbose=verbose)
    from .prerequisites import missing

    if missing():
        # Started from a terminal, this can ask. Started from a login entry there is nobody to ask, so it says what
        # is wrong and stops rather than showing an icon that could never report anything.
        if sys.stdin is not None and sys.stdin.isatty():
            if not offer_to_install():
                return 1
        else:
            names = ", ".join(requirement.name for requirement in missing())
            logger.error("cannot start, these are missing: {}", names)
            print(f"gh-tray cannot start. Missing: {names}. Run 'gh-tray setup' to fix this.", file=sys.stderr)
            return 1
    lock = SingleInstance(LOCK_PATH)
    if not lock.acquire():
        logger.error("another instance is already running")
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 1
    if foreground:
        try:
            run_here()
        finally:
            lock.release()
        return 0
    # Probed only: the tray takes the lock for itself in a moment, and holding it here would keep it out.
    lock.release()
    started = start_detached(launch_command(), STDERR_PATH)
    print(f"{APP_NAME} started as process {started}. Its icon is in the tray, and Quit is in the icon's menu.")
    print(f"Logging to {linked(LOG_PATH)}. Serious errors to {linked(STDERR_PATH)}.")
    return 0


def run_here() -> None:
    """Run the tray in this process until it quits, sending everything it says to the one log."""
    # Imported here rather than at the top, so the commands that open no window never load the toolkit.
    from PySide6.QtCore import qVersion

    from .toolkit import application, route_toolkit_messages
    from .tray import Tray

    # Nobody is watching a tray's console, so whatever would have been printed is logged instead.
    capture_stray_output()
    route_toolkit_messages()
    app = application()
    logger.info("starting {} {}", APP_NAME, __version__)
    logger.debug(
        "on {} through the {} platform, toolkit {} drawing in the {} style, Python {}",
        sys.platform,
        app.platformName(),
        qVersion(),
        app.style().name(),
        sys.version.split()[0],
    )
    Tray().run()


@app.command
def once(verbose: bool = False) -> int:
    """Poll a single time, print the status and any changes, then exit.

    Refuses to run while the tray is up. Both would poll against the same stored comparison point, so whichever ran
    first would consume the changes and the other would never report them.

    :param verbose: write the debug level to the console as well, which shows each search and what it returned
    :return: process exit code, non-zero when the poll failed or the tray is already running
    """
    start_logging(to_console=True, verbose=verbose)
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
    result = poll(load_config())
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


def main() -> None:
    """Run the command line application."""
    sys.exit(app() or 0)


if __name__ == "__main__":
    main()
