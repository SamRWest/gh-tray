"""How a command is handed to a terminal window, and whether that window fills the screen."""

from __future__ import annotations

import pytest

from gh_tray import environment

COMMAND = "gh dash"


def only(name: str, monkeypatch) -> None:
    """Pretend the named program is the only one installed."""
    monkeypatch.setattr(environment.shutil, "which", lambda wanted: f"/usr/bin/{wanted}" if wanted == name else None)


def test_windows_terminal_is_told_to_fill_the_screen(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    only("wt", monkeypatch)
    assert "--fullscreen" in environment.terminal_command(COMMAND, "gh-dash", fullscreen=True)


def test_windows_terminal_opens_at_its_usual_size_when_not_asked(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    only("wt", monkeypatch)
    assert "--fullscreen" not in environment.terminal_command(COMMAND, "gh-dash", fullscreen=False)


def test_the_windows_fallback_maximises_instead(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    monkeypatch.setattr(environment.shutil, "which", lambda _wanted: None)
    assert "/max" in environment.terminal_command(COMMAND, "gh-dash", fullscreen=True)


def test_the_command_always_reaches_the_terminal(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    for present in ("wt", None):
        monkeypatch.setattr(environment.shutil, "which", lambda wanted, name=present: f"C:/{wanted}.exe" if wanted == name else None)
        for fullscreen in (True, False):
            assert COMMAND in environment.terminal_command(COMMAND, "gh-dash", fullscreen=fullscreen)


def test_macos_zooms_the_window_only_when_asked(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "darwin")
    assert "set zoomed" in environment.terminal_command(COMMAND, "gh-dash", fullscreen=True)[-1]
    assert "set zoomed" not in environment.terminal_command(COMMAND, "gh-dash", fullscreen=False)[-1]


def test_a_linux_terminal_gets_its_own_fill_the_screen_flag(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    only("konsole", monkeypatch)
    assert environment.terminal_command(COMMAND, "gh-dash", fullscreen=True)[1] == "--fullscreen"


def test_a_linux_terminal_without_one_still_opens(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    only("xterm", monkeypatch)
    built = environment.terminal_command(COMMAND, "gh-dash", fullscreen=True)
    assert built[0].endswith("xterm")
    assert built[-1] == COMMAND


def test_filling_the_screen_prefers_a_terminal_that_can(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    # Both installed; the plain one comes first in the usual order but cannot fill the screen.
    installed = {"x-terminal-emulator", "gnome-terminal"}
    monkeypatch.setattr(environment.shutil, "which", lambda wanted: f"/usr/bin/{wanted}" if wanted in installed else None)
    assert environment.terminal_command(COMMAND, "gh-dash", fullscreen=True)[0].endswith("gnome-terminal")
    assert environment.terminal_command(COMMAND, "gh-dash", fullscreen=False)[0].endswith("x-terminal-emulator")


def test_no_terminal_at_all_is_reported_rather_than_ignored(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    monkeypatch.setattr(environment.shutil, "which", lambda _wanted: None)
    with pytest.raises(RuntimeError, match="no terminal emulator"):
        environment.terminal_command(COMMAND, "gh-dash", fullscreen=True)
