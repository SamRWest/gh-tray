"""One polling cycle: collect, diff against the last poll, record what changed, and summarise the result."""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from .collector import run_digest
from .config import SNAPSHOT_PATH
from .events import (
    append_events,
    carry_forward,
    carry_known_values,
    detect_events,
    mark_seen,
    mention_urls,
    read_events,
    snapshot_of,
    unread_events,
)
from .status import Status, status_from
from .storage import read_json, write_json_atomic

# Bumped whenever the snapshot's shape changes. A snapshot written by an older version cannot be compared against,
# so it is replaced without reporting the whole of it as new.
SNAPSHOT_VERSION = 3


@dataclass(frozen=True)
class PollResult:
    """What one polling cycle produced."""

    status: Status
    events: list[dict] = field(default_factory=list)
    error: str = ""
    first_run: bool = False


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


def write_snapshot(snapshot: dict) -> None:
    """Write the snapshot this poll will be compared against next time."""
    write_json_atomic(SNAPSHOT_PATH, {"version": SNAPSHOT_VERSION, "entries": snapshot})


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
    previous, damaged = read_snapshot()
    baseline_only = previous is None
    if previous:
        current = carry_known_values(previous, current)
    events = [] if baseline_only else detect_events(previous, current, digest, mention_urls(read_events()))
    append_events(events)
    write_snapshot(carry_forward(previous or {}, current))
    if baseline_only and not damaged:
        # A genuine first run has no history, so there is nothing the user could already have missed.
        mark_seen()
        logger.info("baseline established from {} pull request(s)", len(current))
    elif baseline_only:
        logger.warning("re-established the baseline from {} pull request(s), keeping the unread count", len(current))
    elif events:
        logger.info("detected {} change(s)", len(events))
    return PollResult(status=status_from(digest, unread_events()), events=events, first_run=baseline_only and not damaged)
