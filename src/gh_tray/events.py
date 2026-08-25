"""Detecting, recording and reading changes between polls.

Only transitions produce events. A pull request that was already failing when the previous poll ran is not reported
again, which is what stops a large backlog of red pull requests becoming a wall of notifications.

Polling and looking are tracked separately: the collector advances its baseline every run, but the unread count is
measured against the last time the user actually looked, so nothing is lost between a change landing and being read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from loguru import logger

from .config import EVENTS_PATH, SEEN_PATH

# Each rule names the change it detects, the wording used in menus and notifications, and whether it should turn the
# icon red rather than amber. Red means someone is blocked, or something the user owns is broken.
RULE_LABELS: dict[str, tuple[str, bool]] = {
    "review_requested": ("Review requested", True),
    "ci_broken": ("Checks broke", True),
    "changes_requested": ("Changes requested", True),
    "mention": ("Mentioned", True),
    "ready_to_merge": ("Ready to merge", False),
    "conflict": ("Conflict appeared", False),
    "new_comment": ("New comment", False),
}

BROKEN_CI = frozenset({"FAILURE", "ERROR"})

# What each snapshot field falls back to when the collector reports it as absent. A field left as None would make
# every later comparison against it meaningless, so the fallbacks stand in for "nothing known yet".
SNAPSHOT_DEFAULTS: dict = {
    "repo": "",
    "number": 0,
    "title": "",
    "url": "",
    "ci": "NO_CHECKS",
    "reviewDecision": "NONE",
    "mergeable": "UNKNOWN",
    "comments": 0,
    "isDraft": False,
}
SNAPSHOT_FIELDS = tuple(SNAPSHOT_DEFAULTS)
EVENT_HISTORY_LIMIT = 500

# Microsecond precision, so a change detected in the same second as a manual refresh still sorts after it.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def label_for(kind: str) -> str:
    """Return the human wording for an event kind, falling back to the kind itself if it is unknown."""
    return RULE_LABELS.get(kind, (kind, False))[0]


def is_urgent(kind: str) -> bool:
    """Return whether an event kind means someone is blocked or something is broken."""
    return RULE_LABELS.get(kind, ("", False))[1]


def utc_now() -> str:
    """Return the current time as a sortable UTC timestamp.

    Sub-second precision matters: unread changes are those stamped later than the moment the user last looked, so at
    whole-second resolution a change detected in the same second as a manual refresh would never be counted.
    """
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


def snapshot_of(digest: dict) -> dict:
    """Reduce a digest to the per-pull-request fields worth diffing on the next poll.

    :param digest: a full collector result
    :return: pull requests keyed by repository and number
    """
    snapshot = {}
    for side in ("authored", "reviewing"):
        for pull_request in digest.get(side, []):
            record = {field: default if pull_request.get(field) is None else pull_request[field] for field, default in SNAPSHOT_DEFAULTS.items()}
            record["side"] = side
            snapshot[pull_request["key"]] = record
    return snapshot


def mergeable_now(pull_request: dict) -> bool:
    """Return whether a pull request could be merged exactly as it stands."""
    return (
        pull_request.get("reviewDecision") == "APPROVED"
        and pull_request.get("ci") == "SUCCESS"
        and pull_request.get("mergeable") == "MERGEABLE"
        and not pull_request.get("isDraft")
    )


def _event(kind: str, pull_request: dict, detail: str, at: str) -> dict:
    """Build one event record from a pull request and a description of what changed."""
    return {
        "at": at,
        "kind": kind,
        "key": f"{pull_request['repo']}#{pull_request['number']}",
        "title": pull_request.get("title", ""),
        "url": pull_request.get("url", ""),
        "detail": detail,
    }


def detect_pull_request_events(previous: dict, current: dict, at: str) -> list[dict]:
    """Compare two pull request snapshots and return one event per change worth reporting.

    :param previous: the snapshot written by the last poll
    :param current: the snapshot just built
    :param at: timestamp to stamp on every event
    :return: events, in no particular order
    """
    events: list[dict] = []
    for key, pull_request in current.items():
        was = previous.get(key)
        if was is None:
            if pull_request["side"] == "reviewing":
                events.append(_event("review_requested", pull_request, "added to your review queue", at))
            continue
        if pull_request["side"] == "reviewing" and was.get("side") != "reviewing":
            events.append(_event("review_requested", pull_request, "added to your review queue", at))
        if pull_request["side"] == "authored":
            if pull_request["ci"] in BROKEN_CI and was.get("ci") not in BROKEN_CI:
                events.append(_event("ci_broken", pull_request, f"{str(was.get('ci', '')).lower()} to {pull_request['ci'].lower()}", at))
            if pull_request.get("reviewDecision") == "CHANGES_REQUESTED" and was.get("reviewDecision") != "CHANGES_REQUESTED":
                events.append(_event("changes_requested", pull_request, "a reviewer asked for changes", at))
            if mergeable_now(pull_request) and not mergeable_now(was):
                events.append(_event("ready_to_merge", pull_request, "approved, green and conflict free", at))
        if pull_request.get("mergeable") == "CONFLICTING" and was.get("mergeable") != "CONFLICTING":
            events.append(_event("conflict", pull_request, "needs a rebase", at))
        if (pull_request.get("comments") or 0) > (was.get("comments") or 0):
            events.append(_event("new_comment", pull_request, f"{pull_request['comments'] - (was.get('comments') or 0)} new", at))
    return events


def detect_mention_events(digest: dict, seen_urls: set[str], at: str) -> list[dict]:
    """Return one event per mention not already recorded.

    A mention stays unread on GitHub until it is opened there, so the same one can arrive on several polls.

    :param digest: a full collector result
    :param seen_urls: mention addresses already in the event history
    :param at: timestamp to stamp on every event
    """
    events = []
    for mention in digest.get("mentions", []):
        url = mention.get("url", "")
        if url and url in seen_urls:
            continue
        events.append(
            {
                "at": at,
                "kind": "mention",
                "key": mention.get("repo", "?"),
                "title": mention.get("title", ""),
                "url": url,
                "detail": str(mention.get("reason", "mention")).replace("_", " "),
            }
        )
    return events


def detect_events(previous: dict, current: dict, digest: dict, seen_urls: set[str] | None = None) -> list[dict]:
    """Return every change worth reporting between two polls.

    :param previous: the snapshot written by the last poll
    :param current: the snapshot just built
    :param digest: the full collector result, read for mentions
    :param seen_urls: mention addresses already recorded, so a still-unread mention is not reported twice
    """
    at = utc_now()
    return detect_pull_request_events(previous, current, at) + detect_mention_events(digest, seen_urls or set(), at)


def read_events(limit: int = EVENT_HISTORY_LIMIT) -> list[dict]:
    """Return the most recent events from the log, oldest first.

    :param limit: how many trailing entries to read
    """
    if not EVENTS_PATH.exists():
        return []
    events = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping an unreadable line in the event log")
    return events


def mention_urls(events: list[dict]) -> set[str]:
    """Return the addresses of every mention in a list of events."""
    return {event["url"] for event in events if event.get("kind") == "mention" and event.get("url")}


def append_events(events: list[dict]) -> None:
    """Append events to the log so they survive until the user has seen them."""
    if not events:
        return
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def last_seen() -> str:
    """Return the timestamp at which the user last looked, or an empty string if they never have."""
    if not SEEN_PATH.exists():
        return ""
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8")).get("lastSeenAt", "")
    except json.JSONDecodeError:
        return ""


def mark_seen() -> None:
    """Record that the user has looked, clearing the unread count without discarding the event history."""
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps({"lastSeenAt": utc_now()}) + "\n", encoding="utf-8")


def unread_events() -> list[dict]:
    """Return the events that have arrived since the user last looked, newest first."""
    since = last_seen()
    return [event for event in reversed(read_events()) if event["at"] > since]


def recent_events(count: int = 10) -> list[dict]:
    """Return the newest events regardless of whether they have been seen."""
    return list(reversed(read_events()))[:count]
