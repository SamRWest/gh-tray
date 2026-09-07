"""The record of what every pull request looked like at the last poll, read by both the polling cycle and windows."""

from __future__ import annotations

from loguru import logger

from .config import SNAPSHOT_PATH
from .storage import read_json, write_json_atomic

# Bumped when the stored shape changes, so an older snapshot is replaced rather than reported as all new.
SNAPSHOT_VERSION = 4


def read_snapshot() -> tuple[dict | None, bool]:
    """Return the snapshot written by the previous poll.

    A missing snapshot and a damaged one are told apart, since damage read as fresh would mark changes as seen.

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
