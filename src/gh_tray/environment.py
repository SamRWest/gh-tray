"""Everything platform-specific: locating tools, opening terminals, starting at login and guarding single instance.

Isolating these here keeps the rest of the application free of operating system branching, and gives one place to
look when behaviour differs between Windows, macOS and Linux.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

from . import APP_NAME

# What Windows calls the part of the screen a window may use, with the taskbar left out.
SPI_GETWORKAREA = 0x0030

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


def hidden_window_flags() -> dict[str, int]:
    """Return subprocess keyword arguments that stop Windows flashing a console for a background command.

    :return: flags to splat into a subprocess call, empty on platforms that need none
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def capture_text() -> dict:
    """Return subprocess keyword arguments for reading a command's output as text.

    The encoding is named rather than left to the system. The GitHub tool writes UTF-8 whatever the machine's
    locale says, so on a Windows console reading it as the local codepage turns a tick into ``a-hat`` and would
    mangle any non-English pull request title on its way through.
    """
    return {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


def make_dpi_aware() -> None:
    """Tell Windows this process draws at the real screen resolution.

    Without this, a window on a display scaled above 100% is drawn small and then stretched by the system, which is
    what makes its text look soft. Must be called before any window is built.
    """
    if sys.platform != "win32":
        return
    import ctypes

    for library, function, argument in (("shcore", "SetProcessDpiAwareness", 2), ("user32", "SetProcessDPIAware", None)):
        try:
            entry = getattr(ctypes.windll, library)
            (getattr(entry, function)(argument) if argument is not None else getattr(entry, function)())
        except (AttributeError, OSError):
            continue
        return
    logger.debug("could not ask Windows for a sharp window, text may look soft")


def work_area(fallback: tuple[int, int]) -> tuple[int, int]:
    """Return how much of the screen a window may use, with any taskbar, dock or panel left out.

    Windows will say directly, and is the one that has to be asked: its toolkit reports the whole screen as
    available and a window placed against the bottom of that ends up behind the taskbar. Elsewhere the toolkit's own
    answer already accounts for the desktop's bars, so the caller's fallback is used.

    :param fallback: the width and height to use where the desktop cannot be asked
    :return: the usable width and height in pixels
    """
    if sys.platform != "win32":
        return fallback
    import ctypes
    import ctypes.wintypes

    area = ctypes.wintypes.RECT()
    try:
        told = ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(area), 0)
    except (AttributeError, OSError) as error:
        logger.debug("could not read the usable screen area: {}", error)
        return fallback
    if not told:
        return fallback
    return area.right - area.left, area.bottom - area.top


def github_cli() -> str | None:
    """Return the path to the GitHub command line tool, or None when it is not installed."""
    return shutil.which("gh")


def github_auth_summary() -> str:
    """Return a one-line description of the GitHub sign-in state, for the settings window."""
    github = github_cli()
    if not github:
        return "GitHub CLI (gh) not found on PATH"
    done = subprocess.run([github, "auth", "status"], check=False, **capture_text(), **hidden_window_flags())
    lines = (done.stdout + done.stderr).splitlines()
    summary = next((line.strip() for line in lines if "Logged in" in line), "")
    # The tool prefixes the line with a tick, which says nothing the words do not.
    return summary.lstrip("✓✔* ").strip() if summary else "Not signed in to GitHub"


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
            return [windows_terminal, *(["--maximized"] if maximised else []), "--title", title, "cmd", "/c", command]
        return ["cmd", "/c", "start", *(["/max"] if maximised else []), title, "cmd", "/k", command]
    if sys.platform == "darwin":
        zoom = "\nset zoomed of front window to true" if maximised else ""
        return ["osascript", "-e", f'tell application "Terminal"\ndo script "{command}"\nactivate{zoom}\nend tell']
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
    """Return the command that starts the tray, preferring an interpreter that shows no console window."""
    interpreter = Path(sys.executable)
    if sys.platform == "win32":
        windowless = interpreter.with_name("pythonw.exe")
        if windowless.exists():
            interpreter = windowless
    return [str(interpreter), "-m", "gh_tray"]


def autostart_path() -> Path:
    """Return the file that makes the tray start at login on this platform."""
    home = Path.home()
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.vbs"
    if sys.platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"com.{APP_NAME}.plist"
    return home / ".config" / "autostart" / f"{APP_NAME}.desktop"


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
        # that launchd silently refuses to load.
        plist = {"Label": f"com.{APP_NAME}", "ProgramArguments": list(command), "RunAtLoad": True}
        return plistlib.dumps(plist).decode("utf-8")
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={desktop_entry_exec(command)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


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
        self._handle = None

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
