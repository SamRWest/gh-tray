"""Everything platform-specific: locating tools, opening terminals, starting at login and guarding single instance.

Isolating these here keeps the rest of the application free of operating system branching, and gives one place to
look when behaviour differs between Windows, macOS and Linux.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import TextIO

from loguru import logger

from . import APP_NAME

# What Windows calls UTF-8, which a console has to be put into by number.
UTF8_CODE_PAGE = 65001

# Terminals tried in order on Linux: the name to look for, the flag that opens it maximised where it has one, and
# the arguments it takes before a command. The first one present wins, except that a request to maximise prefers a
# terminal that can. Maximised, not full screen: the window keeps its title bar and the desktop keeps its panels.
LINUX_TERMINALS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("x-terminal-emulator", None, ("-e", "sh", "-c")),
    ("gnome-terminal", "--maximize", ("--", "sh", "-c")),
    ("xfce4-terminal", "--maximize", ("-x", "sh", "-c")),
    ("konsole", None, ("-e", "sh", "-c")),
    ("alacritty", "--option=window.startup_mode=Maximized", ("-e", "sh", "-c")),
    ("kitty", "--start-as=maximized", ("sh", "-c")),
    ("xterm", None, ("-e", "sh", "-c")),
)


def no_console_flag() -> int:
    """Return the flag that stops Windows flashing up a console for a background command.

    :return: the flag, or nothing to ask for on platforms that need none
    """
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run_quietly(command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command without a console window and return what it printed.

    The encoding is named rather than left to the system. The GitHub tool writes UTF-8 whatever the machine's
    locale says, so on a Windows console reading it as the local codepage turns a tick into ``a-hat`` and would
    mangle any non-English pull request title on its way through.

    A command that fails is returned rather than raised on: every caller here reads the exit code itself, since a
    tool that is missing, signed out or rate limited says so on the way out.

    :param command: the program and its arguments
    :param timeout: how long to wait, or None to wait as long as it takes
    :return: the finished command, with its output as text
    """
    return subprocess.run(
        command,
        check=False,
        timeout=timeout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_console_flag(),
    )


# The console handler has to stay referenced for as long as it is registered: Windows calls straight into it, and
# one that has been garbage collected crashes the process instead of stopping it.
_CONSOLE_HANDLERS: list[object] = []


def on_console_interrupt(stop: Callable[[], None]) -> None:
    """Arrange for something to run when the console asks the process to stop, such as Ctrl+C.

    A plain signal handler is not enough for a tray application. It can only run between Python instructions on
    the main thread, and the tray's main thread spends its life blocked inside the desktop's message loop, so
    Ctrl+C would sit undelivered until the next stray mouse movement. Windows offers a console handler instead,
    called on a thread of its own, which works however busy or idle the main thread is. The signal handler is
    still installed as well, for platforms where blocking calls are interrupted and it does fire.

    :param stop: what to run; it must be safe to call from any thread
    """
    signal.signal(signal.SIGINT, lambda _number, _frame: stop())
    if sys.platform != "win32":
        return
    import ctypes
    import ctypes.wintypes

    routine = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)

    @routine
    def handle(_event: int) -> bool:
        """Stop the application, telling Windows the event is dealt with so it does not also kill the process."""
        stop()
        return True

    _CONSOLE_HANDLERS.append(handle)
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handle, True)
    except (AttributeError, OSError) as error:
        logger.debug("could not watch the console for Ctrl+C: {}", error)


def hide_from_dock() -> None:
    """Keep this process out of the macOS Dock and the application switcher.

    A process that draws a window or a menu bar item is given a Dock icon unless it says otherwise, and the tray,
    the hidden changes window and the settings window would each show one reading "Python". Elsewhere there is
    nothing to do.
    """
    if sys.platform != "darwin":
        return
    try:
        # Imported by name, so a type check aimed at another platform does not go looking for a library that only
        # exists on this one.
        appkit = import_module("AppKit")
    except ImportError as error:
        logger.debug("could not keep this process out of the Dock: {}", error)
        return
    appkit.NSApplication.sharedApplication().setActivationPolicy_(appkit.NSApplicationActivationPolicyAccessory)


def github_cli() -> str | None:
    """Return the path to the GitHub command line tool, or None when it is not installed."""
    return shutil.which("gh")


def github_auth_summary() -> str:
    """Return a one-line description of the GitHub sign-in state, for the settings window."""
    github = github_cli()
    if not github:
        return "GitHub CLI (gh) not found on PATH"
    done = run_quietly([github, "auth", "status"])
    lines = (done.stdout + done.stderr).splitlines()
    summary = next((line.strip() for line in lines if "Logged in" in line), "")
    # The tool prefixes the line with a tick, which says nothing the words do not.
    return summary.lstrip("✓✔* ").strip() if summary else "Not signed in to GitHub"


def applescript_string(text: str) -> str:
    """Return text as an AppleScript string literal, so a quote or backslash in it cannot end the string early.

    :param text: the text to quote
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify_by_script(title: str, body: str) -> None:
    """Raise a plain notification through the macOS scripting bridge, which any process may use.

    Nothing can be attached to it: no icon, and no action when it is clicked.

    :param title: the notification's heading
    :param body: the text under it
    """
    script = f"display notification {applescript_string(body)} with title {applescript_string(title)}"
    run_quietly(["osascript", "-e", script])


def in_utf8(command: str) -> str:
    """Return a Windows command that puts the console into UTF-8 before running.

    A console starts on whatever code page the machine's region asks for, while a program that draws itself out of
    box characters and icons writes UTF-8 regardless. The two then disagree and the drawing arrives as rubbish.

    :param command: the shell command to run
    """
    return f"chcp {UTF8_CODE_PAGE} >nul && {command}"


def terminal_command(command: str, title: str, maximised: bool) -> list[str]:
    """Build the argument vector that runs a command in a new terminal window.

    Maximising is best effort. Where the available terminal has no way to do it, the window simply opens at its
    usual size rather than the command failing to run at all.

    :param command: the shell command to run in the new window
    :param title: window title, honoured only where the terminal supports one
    :param maximised: whether the window should open filling the desktop, keeping its title bar
    :return: the argument vector to start
    :raises RuntimeError: when no terminal emulator can be found
    """
    if sys.platform == "win32":
        windows_terminal = shutil.which("wt")
        if windows_terminal:
            return [
                windows_terminal,
                *(["--maximized"] if maximised else []),
                "--title",
                title,
                "cmd",
                "/c",
                in_utf8(command),
            ]
        return ["cmd", "/c", "start", *(["/max"] if maximised else []), title, "cmd", "/k", in_utf8(command)]
    if sys.platform == "darwin":
        zoom = "\nset zoomed of front window to true" if maximised else ""
        return [
            "osascript",
            "-e",
            f'tell application "Terminal"\ndo script {applescript_string(command)}\nactivate{zoom}\nend tell',
        ]
    # A stable sort, so the usual preference order is kept among terminals that are equally able to maximise.
    candidates = sorted(LINUX_TERMINALS, key=lambda entry: entry[1] is None) if maximised else LINUX_TERMINALS
    for name, flag, launch in candidates:
        found = shutil.which(name)
        if not found:
            continue
        return [found, *([flag] if maximised and flag else []), *launch, command]
    raise RuntimeError("no terminal emulator found")


def open_in_terminal(command: str, title: str, maximised: bool = False) -> None:
    """Run a command in a new terminal window, using the first terminal this platform offers.

    :param command: the shell command to run in the new window
    :param title: window title, honoured only where the terminal supports one
    :param maximised: whether the window should open filling the desktop, keeping its title bar
    :raises RuntimeError: when no terminal emulator can be found
    """
    subprocess.Popen(terminal_command(command, title, maximised))


def launch_command() -> list[str]:
    """Return the command that runs the tray in the process started, preferring an interpreter with no console.

    This is what a login entry runs, and what the ordinary start runs as a process of its own.
    """
    interpreter = Path(sys.executable)
    if sys.platform == "win32":
        windowless = interpreter.with_name("pythonw.exe")
        if windowless.exists():
            interpreter = windowless
    return [str(interpreter), "-m", "gh_tray", "--foreground"]


def start_detached(command: list[str], errors: Path) -> int:
    """Start a command that outlives this process and the terminal it came from, and return its process id.

    On Windows the child would otherwise share this console and go with it; elsewhere a session of its own keeps the
    hang-up that closing a terminal sends from reaching it. Its error stream goes to a file, since nobody is watching.

    :param command: the program and its arguments
    :param errors: where to keep whatever the command writes to its error stream
    """
    errors.parent.mkdir(parents=True, exist_ok=True)
    quiet = subprocess.DEVNULL
    with errors.open("w", encoding="utf-8") as kept:
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            child = subprocess.Popen(
                command, stdin=quiet, stdout=quiet, stderr=kept, creationflags=flags, close_fds=True
            )
        else:
            child = subprocess.Popen(
                command, stdin=quiet, stdout=quiet, stderr=kept, start_new_session=True, close_fds=True
            )
    return child.pid


def autostart_path() -> Path:
    """Return the file that makes the tray start at login on this platform.

    The roaming and configuration directories are asked of the environment first, since either can be moved away
    from its usual place under the home directory, and a file written to the usual place would then never be read.
    """
    home = Path.home()
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.vbs"
    if sys.platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"com.{APP_NAME}.plist"
    configuration = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return configuration / "autostart" / f"{APP_NAME}.desktop"


def autostart_enabled() -> bool:
    """Return whether the tray is currently set to start at login."""
    return autostart_path().exists()


def desktop_entry_exec(command: list[str]) -> str:
    """Quote an argument vector for the ``Exec`` line of a desktop entry.

    An unquoted path containing a space is read as two arguments, which makes the entry fail silently at login.

    :param command: the argument vector that starts the tray
    :return: the value for ``Exec=``
    """
    quoted = []
    for part in command:
        if any(character in part for character in ' \t"\\$`'):
            escaped = part
            for character in ("\\", '"', "$", "`"):
                escaped = escaped.replace(character, f"\\{character}")
            quoted.append(f'"{escaped}"')
        else:
            quoted.append(part)
    return " ".join(quoted)


def autostart_body(command: list[str]) -> str:
    """Return the contents of the login-start file for this platform.

    :param command: the argument vector that starts the tray
    """
    if sys.platform == "win32":
        # A script is used rather than a shortcut because it can run the command with its window hidden. Doubling
        # each quote is the VBScript escape, so a path containing spaces survives into the command line quoted.
        quoted = " ".join(f'""{part}""' if " " in part else part for part in command)
        return f'CreateObject("WScript.Shell").Run "{quoted}", 0, False\n'
    if sys.platform == "darwin":
        # Built by the standard library rather than by hand, so a path containing an ampersand cannot produce XML
        # that launchd silently refuses to load. The search path is recorded too: launchd starts things with a bare
        # one, on which a GitHub tool installed by Homebrew is nowhere to be found.
        plist: dict[str, object] = {"Label": f"com.{APP_NAME}", "ProgramArguments": list(command), "RunAtLoad": True}
        if os.environ.get("PATH"):
            plist["EnvironmentVariables"] = {"PATH": os.environ["PATH"]}
        return plistlib.dumps(plist).decode("utf-8")
    entry = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        f"Exec={desktop_entry_exec(command)}",
        "Terminal=false",
        "X-GNOME-Autostart-enabled=true",
    ]
    return "\n".join(entry) + "\n"


def autostart_encoding() -> str:
    """Return the encoding the login-start file must use on this platform.

    Windows Script Host reads a script as the system codepage unless it finds a byte order mark, so a path holding
    any non-ASCII character would otherwise be mangled and the entry would fail at login.
    """
    return "utf-16" if sys.platform == "win32" else "utf-8"


def set_autostart(enabled: bool) -> None:
    """Add or remove the file that starts the tray at login.

    :param enabled: True to start at login, False to stop doing so
    """
    target = autostart_path()
    if not enabled:
        target.unlink(missing_ok=True)
        logger.info("login start removed")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(autostart_body(launch_command()), encoding=autostart_encoding())
    logger.info("login start written to {}", target)


class SingleInstance:
    """An exclusive lock on a file, held for the life of the process so a second tray cannot start.

    File locking is used rather than a stored process id because a stale id can be reused by an unrelated process,
    whereas a lock is released by the operating system as soon as the holder exits, however it exits.
    """

    def __init__(self, path: Path) -> None:
        """:param path: the lock file, created if absent."""
        self.path = path
        # Held open for as long as the lock is, since closing it is what releases it.
        self._handle: TextIO | None = None

    def acquire(self) -> bool:
        """Take the lock.

        :return: True when this process now holds it, False when another instance already does
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        """Drop the lock, if held."""
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None
