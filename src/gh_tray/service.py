"""One polling cycle: collect, diff against the last poll, record what changed, and summarise the result."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from loguru import logger

from .collector import run_digest
from .config import SNAPSHOT_PATH
from .events import (
    append_events,
    detect_events,
    mark_seen,
    mention_urls,
    read_events,
    snapshot_of,
    unread_events,
)
from .status import Status, status_from


@dataclass(frozen=True)
class PollResult:
    """What one polling cycle produced."""

    status: Status
    events: list[dict] = field(default_factory=list)
    error: str = ""
    first_run: bool = False


def read_snapshot() -> dict | None:
    """Return the snapshot written by the previous poll, or None when there has not been one."""
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("previous snapshot was unreadable, treating this poll as a fresh baseline")
        return None


def write_snapshot(snapshot: dict) -> None:
    """Write the snapshot this poll will be compared against next time."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snapshot), encoding="utf-8")


def poll(config: dict) -> PollResult:
    """Run one collection, record any changes, and summarise the result.

    The first poll has nothing to compare against, so it establishes the baseline rather than inventing changes.

    :param config: current settings
    :return: the status, the changes detected this cycle, and any error
    """
    digest, error = run_digest(config)
    if error:
        return PollResult(status=status_from({}, unread_events(), error), error=error)

    current = snapshot_of(digest)
    previous = read_snapshot()
    first_run = previous is None
    events = [] if first_run else detect_events(previous, current, digest, mention_urls(read_events()))
    append_events(events)
    write_snapshot(current)
    if first_run:
        mark_seen()
        logger.info("baseline established from {} pull request(s)", len(current))
    elif events:
        logger.info("detected {} change(s)", len(events))
    return PollResult(status=status_from(digest, unread_events()), events=events, first_run=first_run)
