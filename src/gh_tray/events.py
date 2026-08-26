"""Detecting, recording and reading changes between polls.

Only transitions produce events. A pull request that was already failing when the previous poll ran is not reported
again, which is what stops a large backlog of red pull requests becoming a wall of notifications.

Polling and looking are tracked separately: the collector advances its baseline every run, but the unread count is
measured against what the user has actually looked at, so nothing is lost between a change landing and being read.

What counts as looked at comes from two places. Clicking a row in the window marks that one row, either way, and
those marks are kept per row. Marking everything seen sets a single timestamp, and anything older than it counts as
seen without needing a mark of its own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from loguru import logger

from .config import EVENTS_PATH, SEEN_PATH
from .storage import read_json, write_json_atomic, write_text_atomic

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

# Changes that are not worth telling the user about when the user is the one who made them. A comment they wrote
# themselves is not news; their own commit breaking the checks still is, so only these two are dropped.
SELF_CAUSED_KINDS = frozenset({"new_comment", "mention"})

# Field values that mean "not worked out yet" rather than a real state, and so must never be compared against.
UNINFORMATIVE_VALUES: dict[str, frozenset[str]] = {"mergeable": frozenset({"UNKNOWN"})}

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
    "updatedAt": "",
    "author": "",
    "lastCommitBy": "",
    "lastReviewBy": "",
    "lastCommentBy": "",
}

# Whose name to show against each kind of change. The collector reports the last person to act in each of these
# ways, so the rule that fired decides which of them is the one worth naming. A conflict has nobody to name: it is
# a consequence of somebody else's merge into the branch, which GitHub does not attribute.
ACTOR_FIELDS: dict[str, str] = {
    "review_requested": "author",
    "ci_broken": "lastCommitBy",
    "changes_requested": "lastReviewBy",
    "ready_to_merge": "lastReviewBy",
    "new_comment": "lastCommentBy",
}
SNAPSHOT_FIELDS = tuple(SNAPSHOT_DEFAULTS)

# Microsecond precision, so a change detected in the same second as a manual refresh still sorts after it.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# How many polls a pull request may be missing from the collector's results before it is treated as gone. The
# collector's GitHub queries fail and truncate intermittently, and without this a pull request that blinks out of
# one result would be reported as newly arrived when it came back.
ABSENCE_GRACE_POLLS = 3

# The event log is trimmed to the tail whenever the user marks everything seen, and capped regardless so that never
# looking cannot grow the file without limit.
EVENT_TAIL_KEPT = 200
EVENT_HARD_LIMIT = 2000

# What the record of looking holds: when the user last marked everything seen, and the rows they have since marked
# one at a time. The second is capped, since a row marked long ago has usually dropped out of the window entirely.
LAST_SEEN_KEY = "lastSeenAt"
SEEN_ROWS_KEY = "rows"
SEEN_ROWS_KEPT = 200


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


def moment(stamp: str) -> datetime:
    """Parse a stored timestamp into a comparable moment.

    Timestamps are compared as moments rather than as text because text comparison is only sound while every stamp
    shares one format, and a stamp written by an older version would otherwise sort the wrong way round.

    :param stamp: a timestamp as written into the event log or the seen marker
    :return: the moment it names, or the earliest representable moment when it cannot be read
    """
    try:
        return datetime.strptime(stamp, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("could not read the timestamp {}, treating it as the distant past", stamp)
        return datetime.min.replace(tzinfo=UTC)


def age_in_words(stamp: str, now: datetime | None = None) -> str:
    """Describe how long ago something happened, in the fewest words that stay accurate.

    :param stamp: a timestamp as written into the event log
    :param now: the moment to measure against, defaulting to the present
    :return: a short phrase such as ``just now`` or ``3d ago``
    """
    seconds = max(0.0, ((now or datetime.now(UTC)) - moment(stamp)).total_seconds())
    for limit, divisor, unit in ((90, 1, ""), (3600, 60, "m"), (86400, 3600, "h"), (604800, 86400, "d")):
        if seconds < limit:
            return "just now" if not unit else f"{int(seconds // divisor)}{unit} ago"
    return f"{int(seconds // 604800)}w ago"


def snapshot_key(side: str, key: str) -> str:
    """Return the snapshot key for one pull request on one side of the digest.

    The side is part of the key because the same pull request can appear both as the user's own and as one awaiting
    their review, and collapsing the two would discard whichever arrived first along with its change history.

    :param side: ``authored`` or ``reviewing``
    :param key: the collector's own key, being repository and number
    """
    return f"{side}:{key}"


def snapshot_of(digest: dict) -> dict:
    """Reduce a digest to the per-pull-request fields worth diffing on the next poll.

    :param digest: a full collector result
    :return: pull requests keyed by side, repository and number
    """
    snapshot = {}
    for side in ("authored", "reviewing"):
        for pull_request in digest.get(side, []):
            record = {field: default if pull_request.get(field) is None else pull_request[field] for field, default in SNAPSHOT_DEFAULTS.items()}
            record["side"] = side
            record["absent_polls"] = 0
            snapshot[snapshot_key(side, pull_request["key"])] = record
    return snapshot


def carry_known_values(previous: dict, current: dict) -> dict:
    """Replace values meaning "not worked out yet" with the last value that meant something.

    GitHub works out whether a pull request can be merged only when asked, so it commonly reads as unknown on one
    poll and returns to its real value on the next. Comparing against the unknown would report that return as a
    fresh change, over and over, for a pull request whose state never actually moved.

    :param previous: the snapshot stored by the last poll
    :param current: the snapshot just built from a fresh result
    :return: the fresh snapshot with uninformative values replaced by the last known ones
    """
    filled = {}
    for key, record in current.items():
        was = previous.get(key)
        if was is None:
            filled[key] = record
            continue
        updated = dict(record)
        for field, uninformative in UNINFORMATIVE_VALUES.items():
            if updated.get(field) in uninformative and was.get(field) not in uninformative:
                updated[field] = was[field]
        filled[key] = updated
    return filled


def carry_forward(previous: dict, current: dict, grace: int = ABSENCE_GRACE_POLLS) -> dict:
    """Return the snapshot to store, keeping recently vanished pull requests for a few polls.

    A pull request missing from one collector result is usually a transient GitHub failure rather than a merge, so
    its record is held briefly. Without this it would be reported as newly arrived the moment it came back.

    :param previous: the snapshot stored by the last poll
    :param current: the snapshot just built from a fresh result
    :param grace: how many consecutive polls a pull request may be missing before it is dropped
    :return: the merged snapshot
    """
    merged = dict(current)
    for key, record in previous.items():
        if key in current:
            continue
        absent = record.get("absent_polls", 0) + 1
        if absent <= grace:
            merged[key] = {**record, "absent_polls": absent}
    return merged


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
        "repo": pull_request.get("repo", ""),
        "number": pull_request.get("number", ""),
        "title": pull_request.get("title", ""),
        "url": pull_request.get("url", ""),
        "detail": detail,
        "actor": pull_request.get(ACTOR_FIELDS.get(kind, ""), ""),
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
                "repo": mention.get("repo", ""),
                # The notifications feed names the thread, not the pull request, so there is no number to show.
                "number": "",
                "title": mention.get("title", ""),
                "url": url,
                "detail": str(mention.get("reason", "mention")).replace("_", " "),
                "actor": mention.get("actor", ""),
            }
        )
    return events


def detect_events(previous: dict, current: dict, digest: dict, seen_urls: set[str] | None = None) -> list[dict]:
    """Return every change worth reporting between two polls.

    :param previous: the snapshot written by the last poll
    :param current: the snapshot just built
    :param digest: the full collector result, read for mentions and for who is signed in
    :param seen_urls: mention addresses already recorded, so a still-unread mention is not reported twice
    """
    at = utc_now()
    found = detect_pull_request_events(previous, current, at) + detect_mention_events(digest, seen_urls or set(), at)
    return [event for event in found if not caused_by(event, str(digest.get("viewer", "")))]


def caused_by(event: dict, login: str) -> bool:
    """Return whether a change is one the user brought about themselves.

    Their own comment is not news to them. Their own commit breaking the checks is, so only the kinds where the
    doing and the knowing are the same act are dropped.

    :param event: the change to judge
    :param login: the signed-in account, or empty when it is not known
    """
    return bool(login) and event["kind"] in SELF_CAUSED_KINDS and event.get("actor") == login


def read_events(limit: int | None = None) -> list[dict]:
    """Return events from the log, oldest first.

    The whole log is read by default, because the unread count must cover everything the user has not seen and the
    log is kept small by trimming rather than by reading only part of it.

    :param limit: when given, read only this many trailing entries
    """
    if not EVENTS_PATH.exists():
        return []
    lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines[-limit:] if limit else lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping an unreadable line in the event log")
    return events


def trim_events(keep: int) -> None:
    """Shorten the event log to its most recent entries.

    :param keep: how many entries to leave behind
    """
    if not EVENTS_PATH.exists():
        return
    lines = [line for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) <= keep:
        return
    write_text_atomic(EVENTS_PATH, "\n".join(lines[-keep:]) + "\n")
    logger.info("trimmed the event log from {} to {} entries", len(lines), keep)


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
    trim_events(EVENT_HARD_LIMIT)


def read_seen() -> dict:
    """Return what is recorded about what the user has looked at, or an empty record when nothing is."""
    stored, _damaged = read_json(SEEN_PATH)
    return stored if isinstance(stored, dict) else {}


def last_seen() -> str:
    """Return the timestamp at which the user last looked at everything, or an empty string if they never have."""
    return str(read_seen().get(LAST_SEEN_KEY, ""))


def seen_marks() -> dict[str, dict]:
    """Return the rows the user has marked seen or unseen by hand, keyed by whatever identifies each row."""
    marks = read_seen().get(SEEN_ROWS_KEY)
    return {key: mark for key, mark in marks.items() if isinstance(mark, dict)} if isinstance(marks, dict) else {}


def row_identity(url: str, repo: str = "", number: str | int = "") -> str:
    """Return what a row is remembered by once the user has marked it.

    The address is used where there is one, since it names the thing itself rather than where it happens to sit in
    a list. Where there is none, the repository and number stand in.

    :param url: the page the row leads to
    :param repo: the repository the row is about
    :param number: the pull request number, with or without its leading hash
    """
    if url:
        return url
    tail = str(number).lstrip("#").strip()
    return f"{repo}#{tail}" if tail else repo


def event_identity(event: dict) -> str:
    """Return what identifies the row a change is shown on."""
    return row_identity(str(event.get("url", "")), str(event.get("repo", "")), event.get("number", ""))


def mark_still_applies(mark: dict, at: str) -> bool:
    """Return whether a mark made by hand still describes a row.

    A mark is made against the row as it stood, so anything that happens afterwards undoes it: a pull request
    marked seen and then commented on is something to look at again.

    :param mark: what was recorded when the user marked the row
    :param at: when the row being drawn last changed
    """
    return True if not at else moment(str(mark.get("at", ""))) >= moment(at)


def has_been_seen(identity: str, at: str, marks: dict[str, dict], since: datetime | None) -> bool:
    """Return whether the user has already looked at something.

    A mark made on the row itself wins, being the more deliberate statement of the two. Failing that, anything
    older than the last time the user marked everything seen counts as seen.

    :param identity: what the row is remembered by
    :param at: when the thing being judged last changed
    :param marks: the rows marked by hand, as :func:`seen_marks` returns them
    :param since: when the user last marked everything seen, or None if they never have
    """
    mark = marks.get(identity)
    if mark is not None and mark_still_applies(mark, at):
        return bool(mark.get("seen"))
    return since is not None and moment(at) <= since


def mark_seen() -> None:
    """Record that the user has looked at everything, and shorten the log now that nothing in it is unread.

    Marks made on single rows are dropped, since the one timestamp now says everything they said.
    """
    write_json_atomic(SEEN_PATH, {LAST_SEEN_KEY: utc_now()})
    trim_events(EVENT_TAIL_KEPT)


def newest_marks(marks: dict[str, dict], keep: int = SEEN_ROWS_KEPT) -> dict[str, dict]:
    """Return the most recently made marks, so a tray left running cannot grow the file without limit.

    :param marks: every mark currently recorded
    :param keep: how many to keep
    """
    if len(marks) <= keep:
        return marks
    ordered = sorted(marks.items(), key=lambda pair: moment(str(pair[1].get("marked", ""))), reverse=True)
    return dict(ordered[:keep])


def remember_seen(identity: str, at: str, seen: bool) -> None:
    """Record that the user has marked one row seen or unseen.

    Both answers are stored, because marking a row unseen has to outlast the moment everything was last marked
    seen, which would otherwise put the row straight back to seen.

    :param identity: what the row is remembered by
    :param at: when the row last changed, so a later change undoes the mark
    :param seen: whether the user has now seen it
    """
    document = read_seen()
    marks = dict(seen_marks())
    marks[identity] = {"at": at, "seen": seen, "marked": utc_now()}
    document[SEEN_ROWS_KEY] = newest_marks(marks)
    write_json_atomic(SEEN_PATH, document)


def unread_events() -> list[dict]:
    """Return the events the user has not seen, newest first."""
    since = last_seen()
    marker = moment(since) if since else None
    marks = seen_marks()
    return [event for event in reversed(read_events()) if not has_been_seen(event_identity(event), event["at"], marks, marker)]


def recent_events(count: int = 10) -> list[dict]:
    """Return the newest events regardless of whether they have been seen."""
    return list(reversed(read_events(limit=count)))
