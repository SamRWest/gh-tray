"""The login-start file must be valid for its platform, including for awkward installation paths.

Each case parses the generated file back with the parser that platform actually uses, so a malformed file fails the
test rather than failing silently at login where nobody would see it.
"""

from __future__ import annotations

import plistlib
import shlex

import pytest

from gh_tray import environment

AWKWARD = ["/opt/Program Files/Py & Co/pythonw.exe", "-m", "gh_tray"]
PLAIN = ["/usr/bin/python3", "-m", "gh_tray"]


@pytest.fixture(params=[PLAIN, AWKWARD], ids=["plain-path", "spaces-and-ampersand"])
def command(request) -> list[str]:
    """Run each case against both an ordinary path and one with a space and an ampersand."""
    return request.param


def test_the_linux_entry_parses_back_to_the_original_command(command, monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    body = environment.autostart_body(command)
    exec_line = next(line for line in body.splitlines() if line.startswith("Exec="))
    assert shlex.split(exec_line.removeprefix("Exec=")) == command


def test_the_linux_entry_declares_itself_an_application(command, monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    body = environment.autostart_body(command)
    assert body.startswith("[Desktop Entry]")
    assert "Type=Application" in body


def test_the_macos_agent_parses_back_to_the_original_command(command, monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "darwin")
    parsed = plistlib.loads(environment.autostart_body(command).encode("utf-8"))
    assert parsed["ProgramArguments"] == command
    assert parsed["RunAtLoad"] is True


def test_the_windows_script_quotes_the_command_for_the_shell(command, monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    body = environment.autostart_body(command)
    # Recover the VBScript string literal and undo its doubled-quote escaping, which is what the interpreter does.
    literal = body.split('.Run "', 1)[1].rsplit('", 0, False', 1)[0]
    recovered = shlex.split(literal.replace('""', '"'))
    assert recovered == command


def test_a_windows_path_with_spaces_ends_up_quoted(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    assert '""/opt/Program Files/Py & Co/pythonw.exe""' in environment.autostart_body(AWKWARD)


def test_the_windows_script_is_written_where_the_host_can_read_it(monkeypatch):
    # Windows Script Host reads a script as the system codepage unless it finds a byte order mark.
    monkeypatch.setattr(environment.sys, "platform", "win32")
    assert environment.autostart_encoding() == "utf-16"


def test_other_platforms_write_plain_text(monkeypatch):
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(environment.sys, "platform", platform)
        assert environment.autostart_encoding() == "utf-8"


def test_an_ordinary_argument_is_left_unquoted():
    assert environment.desktop_entry_exec(["python3", "-m", "gh_tray"]) == "python3 -m gh_tray"


def test_writing_and_removing_uses_the_platform_encoding(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "gh-tray.entry"
    monkeypatch.setattr(environment, "autostart_path", lambda: target)
    environment.set_autostart(True)
    assert environment.autostart_enabled() is True
    assert target.read_text(encoding=environment.autostart_encoding()).strip()
    environment.set_autostart(False)
    assert environment.autostart_enabled() is False
