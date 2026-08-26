"""One polling cycle: collect, diff against the last poll, record what changed, and summarise the result."""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from .collector import collect
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
from .snapshot import read_snapshot, write_snapshot
from .status import Status, status_from


@dataclass(frozen=True)
class PollResult:
    """What one polling cycle produced."""

    status: Status
    events: list[dict] = field(default_factory=list)
    error: str = ""
    first_run: bool = False


def poll(config: dict) -> PollResult:
    """Run one collection, record any changes, and summarise the result.

    The first poll has nothing to compare against, so it establishes the baseline rather than inventing changes.

    :param config: current settings
    :return: the status, the changes detected this cycle, and any error
    """
    digest, error = collect(config)
    if digest is None or error:
        return PollResult(status=status_from({}, unread_events(), error), error=error)

    current = snapshot_of(digest)
    previous, damaged = read_snapshot()
    baseline_only = previous is None
    events: list[dict] = []
    if previous is not None:
        current = carry_known_values(previous, current)
        events = detect_events(previous, current, digest, mention_urls(read_events()))
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
