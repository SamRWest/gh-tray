"""Reading and writing the small state files this application keeps.

Every write goes to a temporary file first and is then moved into place. A process killed part way through a plain
write leaves a truncated file behind, and a truncated state file is worse than a missing one: it reads as valid but
incomplete, so the change history it describes is silently wrong.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from loguru import logger

# How many times, and how long apart, to retry moving a finished file into place. On Windows a file another program
# is reading cannot be replaced, and a virus scanner opening everything written is enough to cause that. It lasts a
# few tens of milliseconds, so waiting rides it out; anything longer is a real problem and is raised.
REPLACE_ATTEMPTS = 5
REPLACE_PAUSE_SECONDS = 0.05


def replace_when_free(temporary: Path, path: Path) -> None:
    """Move a finished file into place, waiting briefly if something else is holding the destination.

    :param temporary: the file holding the new contents
    :param path: where it belongs
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            logger.debug("{} is in use, waiting to write it", path.name)
            time.sleep(REPLACE_PAUSE_SECONDS)


def write_text_atomic(path: Path, text: str) -> None:
    """Write text so that readers see either the old contents or the new, never a partial file.

    :param path: the file to replace
    :param text: the contents to write
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    replace_when_free(temporary, path)


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
