"""Reading and writing the small state files this application keeps.

Every write goes to a temporary file first and is then moved into place. A process killed part way through a plain
write leaves a truncated file behind, and a truncated state file is worse than a missing one: it reads as valid but
incomplete, so the change history it describes is silently wrong.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from loguru import logger


def write_text_atomic(path: Path, text: str) -> None:
    """Write text so that readers see either the old contents or the new, never a partial file.

    :param path: the file to replace
    :param text: the contents to write
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_json_atomic(path: Path, value: object, indent: int | None = None) -> None:
    """Write a JSON document so that readers never see a partial file.

    :param path: the file to replace
    :param value: anything serialisable to JSON
    :param indent: passed to the encoder, so settings can stay human-readable and state files stay compact
    """
    write_text_atomic(path, json.dumps(value, indent=indent) + ("\n" if indent else ""))


def read_json(path: Path) -> tuple[object | None, bool]:
    """Read a JSON document.

    The caller usually needs to tell "there is no file yet" from "the file is damaged", because the first is a
    normal starting state and the second must not be mistaken for one.

    :param path: the file to read
    :return: the parsed document and whether the file existed but could not be read
    """
    if not path.exists():
        return None, False
    try:
        return json.loads(path.read_text(encoding="utf-8")), False
    except (json.JSONDecodeError, OSError) as error:
        logger.error("could not read {}: {}", path.name, error)
        return None, True
