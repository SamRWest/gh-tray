"""The record of what every pull request looked like at the last poll.

Kept apart from the polling cycle because more than one part of the application reads it: the cycle compares against
it to find what changed, and the windows read it to show what is true now.
"""

from __future__ import annotations

from loguru import logger

from .config import SNAPSHOT_PATH
from .storage import read_json, write_json_atomic

# Bumped whenever the stored shape changes. A snapshot written by an older version cannot be compared against, so it
# is replaced without reporting the whole of it as new.
SNAPSHOT_VERSION = 4


def read_snapshot() -> tuple[dict | None, bool]:
    """Return the snapshot written by the previous poll.

    A missing snapshot and an unusable one are reported separately, because only the first is a genuine fresh start.
    Treating a damaged file as a fresh start would mark every unread change as seen.

    :return: the stored entries, and whether a snapshot existed but could not be used
    """
    stored, damaged = read_json(SNAPSHOT_PATH)
    if stored is None:
        return None, damaged
    if not isinstance(stored, dict) or stored.get("version") != SNAPSHOT_VERSION:
        logger.warning("the stored snapshot is not one this version can compare against, starting a new baseline")
        return None, True
    entries = stored.get("entries")
    return (entries, False) if isinstance(entries, dict) else (None, True)


def write_snapshot(entries: dict) -> None:
    """Write the snapshot the next poll will be compared against."""
    write_json_atomic(SNAPSHOT_PATH, {"version": SNAPSHOT_VERSION, "entries": entries})
