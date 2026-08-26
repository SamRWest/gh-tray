"""Every command can reach what it imports.

The commands that open a window import it inside the function, so the cost of a window toolkit is not paid by a
command that never opens one. The price of that is a rename which moves the target going unnoticed: nothing fails
until somebody runs the command, and the window opens as its own process with no console, so the failure is
silent. These cases resolve each of those imports without running anything.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from gh_tray import __main__

SOURCE = Path(__main__.__file__)


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
    for command in ("once", "settings", "popup", "setup"):
        assert command in __main__.app, f"{command} is not a command"
