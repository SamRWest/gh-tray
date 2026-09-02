"""The login-start file must be valid for its platform, including for awkward installation paths.

Each case parses the generated file back with the parser that platform actually uses, so a malformed file fails the
test rather than failing silently at login where nobody would see it.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil

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


def test_the_windows_entry_follows_a_moved_roaming_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert environment.autostart_path().is_relative_to(tmp_path)


def test_the_linux_entry_follows_a_moved_configuration_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert environment.autostart_path() == tmp_path / "autostart" / "gh-tray.desktop"


def test_the_macos_agent_records_the_search_path(monkeypatch):
    # launchd starts things with a bare search path, on which a GitHub tool installed by Homebrew is nowhere to be
    # found.
    monkeypatch.setattr(environment.sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    parsed = plistlib.loads(environment.autostart_body(PLAIN).encode("utf-8"))
    assert parsed["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}


@pytest.mark.skipif(not shutil.which("desktop-file-validate"), reason="the desktop entry validator is not installed")
def test_the_linux_entry_passes_the_desktop_entry_validator(command, monkeypatch, tmp_path):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    target = tmp_path / "gh-tray.desktop"
    target.write_text(environment.autostart_body(command), encoding="utf-8")
    checked = environment.run_quietly(["desktop-file-validate", str(target)])
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.skipif(not shutil.which("plutil"), reason="the property list checker is only on macOS")
def test_the_macos_agent_passes_the_property_list_checker(command, monkeypatch, tmp_path):
    monkeypatch.setattr(environment.sys, "platform", "darwin")
    target = tmp_path / "com.gh-tray.plist"
    target.write_text(environment.autostart_body(command), encoding="utf-8")
    checked = environment.run_quietly(["plutil", "-lint", str(target)])
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.skipif(not shutil.which("cscript"), reason="the script host is only on Windows")
def test_the_windows_script_runs_under_the_script_host(monkeypatch, tmp_path):
    # Run for real with a command that does nothing, so a script the host cannot parse fails here rather than at login.
    monkeypatch.setattr(environment.sys, "platform", "win32")
    target = tmp_path / "gh-tray.vbs"
    target.write_text(environment.autostart_body(["cmd", "/c", "exit"]), encoding=environment.autostart_encoding())
    checked = environment.run_quietly(["cscript", "//nologo", str(target)])
    assert checked.returncode == 0, checked.stdout + checked.stderr
