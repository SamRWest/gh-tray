"""Starting the tray: detached by default and said so, in the foreground on request, and refused while one runs."""

from __future__ import annotations

import pytest

from gh_tray import __main__ as commands
from gh_tray import prerequisites
from gh_tray.environment import SingleInstance


@pytest.fixture
def ready(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, "LOCK_PATH", tmp_path / "gh-tray.lock")
    monkeypatch.setattr(commands, "STDERR_PATH", tmp_path / "errors.log")
    monkeypatch.setattr(commands, "start_logging", lambda to_console, verbose=False: None)
    monkeypatch.setattr(prerequisites, "missing", list)
    return tmp_path


def test_the_tray_is_started_on_its_own_and_the_terminal_is_told(ready, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(commands, "start_detached", lambda command, errors: started.append(command) or 4242)
    assert commands.run_tray() == 0
    assert started[0][-3:] == ["-m", "gh_tray", "--foreground"]
    said = capsys.readouterr().out
    assert "started" in said
    assert "4242" in said
    # The lock is only probed here, so the tray started can take it.
    assert SingleInstance(commands.LOCK_PATH).acquire()


def test_the_foreground_runs_the_tray_here_and_lets_the_lock_go_after(ready, monkeypatch):
    ran = []
    monkeypatch.setattr(commands, "run_here", lambda: ran.append(True))
    assert commands.run_tray(foreground=True) == 0
    assert ran == [True]
    assert SingleInstance(commands.LOCK_PATH).acquire()


def test_a_second_start_is_refused_while_one_runs(ready, monkeypatch, capsys):
    holder = SingleInstance(commands.LOCK_PATH)
    assert holder.acquire()
    monkeypatch.setattr(commands, "start_detached", lambda command, errors: pytest.fail("nothing should start"))
    assert commands.run_tray() == 1
    assert "already running" in capsys.readouterr().err
    holder.release()
