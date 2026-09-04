"""Every command can reach what it imports.

The commands that open a window import it inside the function, so the cost of a window toolkit is not paid by a
command that never opens one. The price of that is a rename which moves the target going unnoticed: nothing fails
until somebody runs the command, and the window opens as its own process with no console, so the failure is
silent. These cases resolve each of those imports without running anything.
"""

from __future__ import annotations

import ast
import importlib
import io
import sys
import threading
from pathlib import Path

import pytest

from gh_tray import __main__

SOURCE = Path(__main__.__file__)


def logged(records: list[str], level: str = "WARNING") -> int:
    """Add a log sink that collects plain messages, returning its handle for removal.

    :param records: where each message lands
    :param level: the least serious level to collect
    """
    return __main__.logger.add(lambda message: records.append(message.record["message"]), level=level)


def test_stray_stderr_lines_land_in_the_log_once_complete():
    records: list[str] = []
    handle = logged(records)
    stream = __main__.LinesToLog()
    try:
        stream.write("half a ")
        assert records == [], "an unfinished line is not yet a message"
        stream.write("line\nand another\n")
    finally:
        __main__.logger.remove(handle)
    assert records == ["stderr: half a line", "stderr: and another"]


def test_a_write_made_while_logging_goes_to_the_real_stream_rather_than_round_again(monkeypatch):
    net = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", net)
    stream = __main__.LinesToLog()
    stream.forwarding.busy = True
    stream.write("the log itself complaining\n")
    assert net.getvalue() == "the log itself complaining\n"


def test_an_uncaught_exception_is_recorded():
    records: list[str] = []
    handle = logged(records, level="ERROR")
    try:
        __main__.log_uncaught(ValueError, ValueError("boom"), None)
        __main__.log_uncaught_in_thread(threading.ExceptHookArgs((ValueError, ValueError("boom"), None, None)))
    finally:
        __main__.logger.remove(handle)
    assert records == ["uncaught in the main thread", "uncaught in an unnamed thread"]


def test_capturing_installs_the_hooks_and_the_stream(monkeypatch):
    # Registered at their current values, so the capture is undone when the test ends.
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    __main__.capture_stray_output()
    assert isinstance(sys.stderr, __main__.LinesToLog)
    assert sys.excepthook is __main__.log_uncaught
    assert threading.excepthook is __main__.log_uncaught_in_thread


def test_linked_prints_the_whole_path_where_nobody_draws_links(tmp_path):
    # pytest's captured standard output is not a terminal, which is the case the plain path is for.
    target = tmp_path / "gh-tray.log"
    assert __main__.linked(target) == str(target)


def test_linked_wraps_the_name_in_a_terminal_hyperlink(tmp_path, monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdout", Terminal())
    target = tmp_path / "gh-tray.log"
    assert __main__.linked(target) == f"\x1b]8;;{target.as_uri()}\x1b\\gh-tray.log\x1b]8;;\x1b\\"


def deferred_imports() -> list[tuple[str, str, int]]:
    """Return every import written inside a function body in the command line module.

    :return: the module imported from, the name taken out of it, and the line it is on
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found = []
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        for node in ast.walk(function):
            if isinstance(node, ast.ImportFrom) and node.level:
                found += [(node.module or "", alias.name, node.lineno) for alias in node.names]
    return found


def test_there_are_deferred_imports_to_check():
    # If this ever finds none, the cases below would pass by doing nothing at all.
    assert deferred_imports()


@pytest.mark.parametrize("module, name, line", deferred_imports(), ids=lambda value: str(value))
def test_a_deferred_import_resolves(module, name, line):
    imported = importlib.import_module(f"gh_tray.{module}")
    assert hasattr(imported, name), f"{SOURCE.name} line {line} imports {name} from {module}, which does not have it"


def test_every_command_is_reachable():
    for command in ("once", "settings", "setup"):
        assert command in __main__.app, f"{command} is not a command"
