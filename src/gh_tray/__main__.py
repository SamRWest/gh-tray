"""Command line entry point.

Running with no command shows the tray. ``once`` polls a single time and prints the result, which is the quickest
way to check the collector and the GitHub sign-in. ``settings`` opens the settings window on its own.
"""

from __future__ import annotations

import sys

import cyclopts
from loguru import logger

from . import APP_NAME, __version__
from .config import APP_DIR, LOCK_PATH, LOG_PATH, load_config
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


LIGHTS = ("🟢", "🟡", "🔴")
PLAIN_LIGHTS = ("[ ok ]", "[ -- ]", "[ !! ]")


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
    print(f"{APP_NAME} needs these:\n")
    for requirement, present in requirements():
        light = present_mark if present else (installable_mark if requirement.installable else manual_mark)
        note = requirement.summary if present else (" ".join(requirement.command) or requirement.manual)
        print(f"  {light}  {requirement.name:<14} {note}")
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
def run_tray() -> int:
    """Show the tray icon and poll on a timer.

    :return: process exit code
    """
    start_logging(to_console=False)
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
    try:
        # Imported here rather than at the top, so the commands that open no window never load the toolkit.
        from .toolkit import application
        from .tray import Tray

        application()
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
