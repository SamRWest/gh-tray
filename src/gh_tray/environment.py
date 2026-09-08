"""Platform-specific code: locating tools, opening terminals, starting at login and guarding single instance.

Isolated here so platform branching does not spread through the rest of the app.
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

from gh_tray import APP_NAME

# What Windows calls UTF-8, which a console has to be put into by number.
UTF8_CODE_PAGE = 65001

# Tried in order; first present wins. A maximised window still keeps its title bar, unlike full screen.
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

    Encoding is fixed at UTF-8, matching the GitHub tool's output, so the system codepage cannot mangle a tick or title.
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


# Must stay referenced while registered: a garbage-collected handler crashes the process instead of stopping it.
_CONSOLE_HANDLERS: list[object] = []


def on_console_interrupt(stop: Callable[[], None]) -> None:
    """Arrange for something to run when the console asks the process to stop, such as Ctrl+C.

    A signal handler alone cannot work: it only fires between instructions on the main thread, which the desktop's
    message loop blocks. Windows instead gets a console handler on its own thread; the signal handler stays too.
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

    Without this, the tray and its hidden windows would each show a Dock icon labelled "Python". No-op elsewhere.
    """
    if sys.platform != "darwin":
        return
    try:
        appkit = import_module("AppKit")
    except ImportError as error:
        logger.debug("could not keep this process out of the Dock: {}", error)
        return
    appkit.NSApplication.sharedApplication().setActivationPolicy_(appkit.NSApplicationActivationPolicyAccessory)


def github_cli() -> str | None:
    """Return the path to the GitHub command line tool, or None when it is not installed."""
    return shutil.which("gh")


def github_auth_state() -> tuple[bool, str]:
    """Return whether the GitHub command line tool is signed in, and one line saying as whom."""
    github = github_cli()
    if not github:
        return False, "GitHub CLI (gh) not found on PATH"
    done = run_quietly([github, "auth", "status"])
    lines = (done.stdout + done.stderr).splitlines()
    summary = next((line.strip() for line in lines if "Logged in" in line), "")
    if not summary:
        return False, "Not signed in to GitHub"
    # The tool prefixes the line with a tick, which says nothing the words do not.
    return done.returncode == 0, summary.lstrip("✓✔* ").strip()


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

    A console starts on the machine's code page, but box-drawn output is UTF-8, so mismatched it renders as rubbish.
    :param command: the shell command to run
    """
    return f"chcp {UTF8_CODE_PAGE} >nul && {command}"


def terminal_command(command: str, title: str, maximised: bool) -> list[str]:
    """Build the argument vector that runs a command in a new terminal window.

    Maximising is best effort: a terminal without support just opens at its usual size instead of failing.
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
    """Return the command that runs the tray in the process started, preferring an interpreter with no console."""
    interpreter = Path(sys.executable)
    if sys.platform == "win32":
        windowless = interpreter.with_name("pythonw.exe")
        if windowless.exists():
            interpreter = windowless
    return [str(interpreter), "-m", "gh_tray", "--foreground"]


def start_detached(command: list[str], errors: Path) -> int:
    """Start a command that outlives this process and the terminal it came from, and return its process id.

    Without this the child would share this console on Windows or get the hang-up a closed terminal sends elsewhere.
    :param command: the program and its arguments
    :param errors: where to keep whatever the command writes to its error stream
    """
    errors.parent.mkdir(parents=True, exist_ok=True)
    logger.debug("starting on its own: {}", " ".join(command))
    quiet = subprocess.DEVNULL
    with errors.open("w", encoding="utf-8") as kept:
        if sys.platform == "win32":
            # Gives the child a hidden console; a venv's own interpreter inherits it, else it opens a visible one.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
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

    Checks the environment for the roaming or config directory first, since either can move from its default place.
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
        # Not a shortcut: a script can run with a hidden window. Doubling each quote is the VBScript escape for spaces.
        quoted = " ".join(f'""{part}""' if " " in part else part for part in command)
        return f'CreateObject("WScript.Shell").Run "{quoted}", 0, False\n'
    if sys.platform == "darwin":
        # plistlib avoids XML-escaping bugs from a stray ampersand; PATH is added since launchd's own PATH misses
        # a Homebrew-installed GitHub tool.
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

    Windows Script Host reads by system codepage unless it finds a byte order mark, so a non-ASCII path would mangle.
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

    A lock beats a stored process id, since a stale id can be reused; the OS releases it however the holder exits.
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
