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

# Terminals tried in order on Linux. The first one present wins; gnome-terminal needs a different argument form.
LINUX_TERMINALS = ("x-terminal-emulator", "gnome-terminal", "konsole", "alacritty", "kitty", "xfce4-terminal", "xterm")


def hidden_window_flags() -> dict[str, int]:
    """Return subprocess keyword arguments that stop Windows flashing a console for a background command.

    :return: flags to splat into a subprocess call, empty on platforms that need none
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def github_cli() -> str | None:
    """Return the path to the GitHub command line tool, or None when it is not installed."""
    return shutil.which("gh")


def find_bash(configured: str = "") -> str | None:
    """Locate a bash interpreter.

    A configured path that does not exist is rejected rather than passed on, so a mistyped setting is reported as a
    missing interpreter instead of surfacing later as an operating system error from the collector.

    :param configured: an explicit path from the settings, preferred when it points at something real
    :return: a path to bash, or None when none can be found
    """
    if configured:
        if Path(configured).expanduser().exists():
            return str(Path(configured).expanduser())
        logger.warning("the configured bash path does not exist, falling back to discovery: {}", configured)
    found = shutil.which("bash")
    if found:
        return found
    # Windows has no system bash, but Git for Windows ships one two levels up from git.exe.
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    return None


def detect_orgs() -> str:
    """Return the organisations the signed-in account belongs to, as a comma separated list.

    Used to fill the setting on first run so the application is useful before anyone opens the settings window.

    :return: comma separated organisation logins, empty when the tool is missing or the call fails
    """
    github = github_cli()
    if not github:
        return ""
    done = subprocess.run(
        [github, "api", "user/orgs", "--jq", '[.[].login] | join(",")'],
        capture_output=True,
        text=True,
        check=False,
        **hidden_window_flags(),
    )
    if done.returncode != 0:
        logger.warning("could not read organisation memberships: {}", done.stderr.strip()[:200])
        return ""
    return done.stdout.strip()


def github_auth_summary() -> str:
    """Return a one-line description of the GitHub sign-in state, for the settings window."""
    github = github_cli()
    if not github:
        return "GitHub CLI (gh) not found on PATH"
    done = subprocess.run([github, "auth", "status"], capture_output=True, text=True, check=False, **hidden_window_flags())
    lines = (done.stdout + done.stderr).splitlines()
    return next((line.strip() for line in lines if "Logged in" in line), "Not signed in to GitHub")


def open_in_terminal(command: str, title: str) -> None:
    """Run a command in a new terminal window, using the first terminal this platform offers.

    :param command: the shell command to run in the new window
    :param title: window title, honoured only where the terminal supports one
    :raises RuntimeError: when no terminal emulator can be found
    """
    if sys.platform == "win32":
        windows_terminal = shutil.which("wt")
        if windows_terminal:
            subprocess.Popen([windows_terminal, "--title", title, "cmd", "/c", command])
        else:
            subprocess.Popen(["cmd", "/c", "start", title, "cmd", "/k", command])
        return
    if sys.platform == "darwin":
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{command}"'])
        return
    for terminal in LINUX_TERMINALS:
        found = shutil.which(terminal)
        if not found:
            continue
        arguments = [found, "--", "sh", "-c", command] if terminal == "gnome-terminal" else [found, "-e", command]
        subprocess.Popen(arguments)
        return
    raise RuntimeError("no terminal emulator found")


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
