"""What the changes window lists: the rows, in what order, in which inks, and what is remembered about them.

The list is what changed since the user last looked, then what is merely waiting on them. Changes alone would leave
the window saying "nothing" on a quiet day while three reviews sat in the queue, which is the opposite of useful.

A row is drawn as seen once it has been marked, and as unseen again if anything happens to it afterwards.

Nothing here draws. Rows carry the names of the inks they are drawn in, and the window looks each up in the palette
of whichever theme the desktop is set to at the time, so following the desktop from dark to light never means
building the rows again.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .events import (
    BROKEN_CI,
    age_in_words,
    event_identity,
    has_been_seen,
    is_urgent,
    label_for,
    last_seen,
    mergeable_now,
    moment,
    recent_events,
    remember_seen,
    row_identity,
    seen_marks,
)
from .snapshot import read_snapshot
from .theme import Palette, blend, ink

# The inks a row can be drawn in, by the names the palette knows them under: what blocks somebody, what is worth a
# look, what is good news, and what nobody need act on.
URGENT = "red"
ROUTINE = "amber"
GOOD = "green"
QUIET = "muted"

# How strongly a row is drawn once the user has seen it. This is the only thing that dims a row: how old something
# is has a scale of its own in the date column, and dimming for that as well left two rows of the same sort looking
# different for a reason nobody could name.
SEEN_STRENGTH = 0.58

# One hue per sort of change, so a glance down the window tells them apart before a word is read. Anything not
# named here falls back to red when it blocks somebody and amber otherwise.
KIND_COLOURS: dict[str, str] = {
    "review_requested": "orange",
    "ci_broken": "red",
    "changes_requested": "amber",
    "mention": "violet",
    "ready_to_merge": "green",
    "conflict": "pink",
    "new_comment": "blue",
}

# Each column: the name it is known by, its heading, how many characters wide it starts, and whether it takes the
# space a wider window adds. Every one can be resized afterwards by dragging the divider in its heading.
COLUMNS: tuple[tuple[str, str, int, bool], ...] = (
    ("change", "Change", 23, False),
    ("org", "Org", 16, False),
    ("repo", "Repo", 26, False),
    ("pr", "PR", 7, False),
    ("status", "Status", 10, False),
    ("title", "Title", 44, True),
    ("author", "Author", 16, False),
    ("who", "Who", 16, False),
    ("when", "When", 10, False),
)
DEFAULT_SORT = "when"

# How each column sorts when its heading is clicked. Dates sort as moments and numbers as numbers, because sorting
# either as the text shown would put "3m ago" beside "3w ago" and "#7" after "#128".
SORT_KEYS = {
    "change": lambda row: row.label.casefold(),
    "org": lambda row: org_and_name(row.repo)[0].casefold(),
    "repo": lambda row: org_and_name(row.repo)[1].casefold(),
    "pr": lambda row: pull_request_number(row.number),
    "status": lambda row: row.status.casefold(),
    "title": lambda row: row.title.casefold(),
    "author": lambda row: (not row.author, row.author.casefold()),
    "who": lambda row: (not row.who, row.who.casefold()),
    "when": lambda row: moment(row.at),
}


TITLE_LIMIT = 90
# How many log entries to read per row shown, since several entries about one pull request collapse into one row.
ROWS_READ_DEEPLY = 5
# The age at which a date is drawn at the far end of its colour scale.
AGE_RAMP_DAYS = 365


@dataclass(frozen=True)
class Row:
    """One line of the table, whatever it was built from."""

    label: str
    repo: str
    number: str
    title: str
    who: str
    when: str
    url: str
    # The name of the ink the row is drawn in, looked up in the palette of whichever theme is current when it is
    # drawn.
    colour: str
    at: str = ""
    seen: bool = False
    # Whose the pull request is, as opposed to who triggered the change the row reports. Emily's pull request can
    # carry a comment from somebody else, and showing only one of the two names misleads about the other.
    author: str = ""
    # Which of the user's hats the row lands on: ``author`` of the pull request, ``reviewer`` of it, or the target
    # of a ``mention``. What the window's quick filters go by.
    role: str = ""
    # How the pull request stands right now, as :func:`pull_request_status` words it, or empty when its state is
    # not known. What the Status column shows and the closed filter goes by.
    status: str = ""


# The inks a name can be drawn in. Every name, whether a person's, an organisation's or a repository's, is dealt one
# by a stable digest of its spelling, so the same name reads as the same colour in every row, every showing and every
# restart, and rows about the same organisation or repository group by eye. In these columns a colour is an identity
# tag and carries none of the meaning the Change column gives it.
NAME_COLOURS: tuple[str, ...] = ("blue", "green", "violet", "orange", "pink", "amber", "red")


def name_colour(name: str) -> str:
    """Return the name of the ink a name is drawn in, the same one every time for the same name.

    :param name: the name to colour; an empty one gets the quiet ink, though there is nothing to draw anyway
    """
    if not name:
        return QUIET
    return NAME_COLOURS[zlib.crc32(name.encode("utf-8")) % len(NAME_COLOURS)]


# The mark at the head of a row: filled while it still wants attention, hollow once seen. A plain shape rather than
# a coloured emoji, which is drawn from the font in one colour whatever the character is. Drawn in the row's own
# colour, this one is the filled colour those were meant to be.
UNSEEN_GLYPH = "●"
SEEN_GLYPH = "○"
GLYPHS = (UNSEEN_GLYPH, SEEN_GLYPH)


def glyph_for(entry: Row) -> str:
    """Return the mark that heads a row.

    :param entry: the row to mark
    """
    return SEEN_GLYPH if entry.seen else UNSEEN_GLYPH


# What each standing state is called, whose name goes beside it, whether it blocks somebody, and the ink it is drawn
# in. These describe how a pull request is right now, unlike the event labels, which describe what just happened.
# Something ready to merge is good news rather than a warning, so it is green.
STANDING_STATES: tuple[tuple[str, str, str, bool, str], ...] = (
    ("reviewing", "Awaiting your review", "author", True, "orange"),
    ("changes_requested", "Changes requested", "lastReviewBy", True, "amber"),
    ("checks_failing", "Checks failing", "lastCommitBy", True, "red"),
    ("ready_to_merge", "Ready to merge", "lastReviewBy", False, "green"),
)


def org_and_name(repo: str) -> tuple[str, str]:
    """Split a repository's full name into who owns it and what it is called.

    :param repo: the full name, such as ``acme/widget``
    :return: the owner and the name; a name with no owner in it comes back whole, owned by nobody
    """
    owner, slash, name = repo.partition("/")
    return (owner, name) if slash else ("", repo)


def pull_request_number(shown: str) -> int:
    """Return a pull request number as a number, so a column of them sorts by size rather than by spelling.

    :param shown: the number as the table shows it, such as ``#128``
    """
    digits = shown.lstrip("#").strip()
    return int(digits) if digits.isdigit() else 0


def days_old(stamp: str, now: datetime | None = None) -> float:
    """Return how many days ago something happened.

    :param stamp: when it happened
    :param now: the moment to measure against, defaulting to the present
    """
    if not stamp:
        return 0.0
    return max(0.0, ((now or datetime.now(UTC)) - moment(stamp)).total_seconds()) / 86400


def age_colour(stamp: str, inks: Palette, now: datetime | None = None) -> str:
    """Return the colour a date is drawn in: blue for just-happened, through violet, to red for long-forgotten.

    The scale is by the logarithm of the age rather than the age itself, because the difference between an hour and
    a day matters and the difference between forty and fifty weeks does not. It runs through violet rather than
    straight from one end to the other, because mixing blue directly into red passes through grey and the middle of
    the scale stops saying anything.

    :param stamp: when it happened
    :param inks: the palette of the theme being drawn in
    :param now: the moment to measure against, defaulting to the present
    """
    along = min(1.0, math.log1p(days_old(stamp, now)) / math.log1p(AGE_RAMP_DAYS))
    if along < 0.5:
        return blend(inks.fresh, inks.violet, 1.0 - along * 2)
    return blend(inks.violet, inks.stale, 2.0 - along * 2)


def repo_and_number(event: dict) -> tuple[str, str]:
    """Return a change's repository and pull request number as separate values.

    Older entries in the log carry only the two joined together, so those are split rather than shown blank.

    :param event: the change to describe
    :return: the repository, and the number prefixed with a hash, either of which may be empty
    """
    repo = event.get("repo") or ""
    number = event.get("number")
    if not repo:
        repo, _, number = str(event.get("key", "")).partition("#")
    if not number:
        # Rows recorded before mentions carried a number still hold the page they lead to, which names it.
        tail = str(event.get("url", "")).rstrip("/").rsplit("/", 1)[-1]
        number = tail if tail.isdigit() else ""
    return repo, f"#{number}" if number else ""


def dot_colour(event: dict) -> str:
    """Return the name of the ink a change is drawn in, which is decided by what sort of change it is.

    A change already seen keeps this ink and is dimmed instead. Turning it grey would say the row had been switched
    off rather than merely read, and would lose what sort of thing it was at a glance.

    :param event: the change the row describes
    """
    return KIND_COLOURS.get(event["kind"], URGENT if is_urgent(event["kind"]) else ROUTINE)


def row_from_event(event: dict, seen: bool) -> Row:
    """Build a row describing something that happened.

    :param event: the change, as recorded in the log
    :param seen: whether the user has already looked at it
    """
    repo, number = repo_and_number(event)
    return Row(
        label=label_for(event["kind"]),
        repo=repo,
        number=number,
        title=str(event.get("title") or event.get("detail", ""))[:TITLE_LIMIT],
        who=str(event.get("actor", "")),
        when=age_in_words(event["at"]),
        url=str(event.get("url", "")),
        colour=dot_colour(event),
        seen=seen,
        at=str(event.get("at", "")),
        author=str(event.get("author", "")),
        # Rows recorded before roles were kept still say what kind of change they are, which names the hat for a
        # mention outright and leaves the rest to be filled from the last poll's records.
        role=str(event.get("role", "")) or ("mention" if event.get("kind") == "mention" else ""),
    )


def standing_state(entry: dict) -> tuple[str, str, bool, str] | None:
    """Return how a pull request stands, when that is something worth acting on.

    :param entry: one pull request as the last poll recorded it
    :return: its label, whose name to show, whether it blocks and the name of its ink, or None when nothing is wanted
    """
    for state, label, who_field, urgent, colour in STANDING_STATES:
        matches = {
            "reviewing": entry.get("side") == "reviewing",
            "changes_requested": entry.get("side") == "authored" and entry.get("reviewDecision") == "CHANGES_REQUESTED",
            "checks_failing": entry.get("side") == "authored" and entry.get("ci") in BROKEN_CI,
            "ready_to_merge": entry.get("side") == "authored" and mergeable_now(entry),
        }[state]
        if matches:
            return label, str(entry.get(who_field, "")), urgent, colour
    return None


def rows_from_snapshot(entries: dict, already_listed: set[str], marks: dict[str, dict] | None = None) -> list[Row]:
    """Build rows for the pull requests that want something from the user right now.

    These fill the window when little has changed lately, so it never says "nothing" while a review is waiting.

    Only a mark on the row itself dims one of these. A review that has been waiting a fortnight is still waiting,
    however long ago the user last cleared the list, so the moment of that clearing says nothing about it.

    :param entries: pull requests as the last poll recorded them
    :param already_listed: addresses of pull requests a change has already put in the list
    :param marks: the rows the user has marked by hand, as :func:`gh_tray.events.seen_marks` returns them
    :return: rows, blocking ones first and most recently touched first within that
    """
    rows = []
    for entry in entries.values():
        standing = standing_state(entry)
        url = str(entry.get("url", ""))
        if standing is None or (url and url in already_listed):
            continue
        label, who, urgent, colour = standing
        touched = str(entry.get("updatedAt", ""))
        identity = row_identity(url, str(entry.get("repo", "")), entry.get("number", ""))
        rows.append(
            (
                not urgent,
                touched,
                Row(
                    label=label,
                    repo=str(entry.get("repo", "")),
                    number=f"#{entry.get('number')}" if entry.get("number") else "",
                    title=str(entry.get("title", ""))[:TITLE_LIMIT],
                    who=who,
                    when=age_in_words(touched) if touched else "",
                    url=url,
                    colour=colour,
                    at=touched,
                    seen=has_been_seen(identity, touched, marks or {}, None),
                    author=str(entry.get("author", "")),
                    role="author" if entry.get("side") == "authored" else "reviewer",
                ),
            )
        )
    rows.sort(key=lambda row: (row[0], [-ord(character) for character in row[1]]))
    return [row for _urgent, _touched, row in rows]


def sorted_rows(rows: list[Row], column: str = DEFAULT_SORT, newest_first: bool = True) -> list[Row]:
    """Return rows in the order a column asks for.

    :param rows: the rows to order
    :param column: which column to order by
    :param newest_first: whether to reverse the column's natural order, which for dates puts the newest at the top
    """
    return sorted(rows, key=SORT_KEYS.get(column, SORT_KEYS[DEFAULT_SORT]), reverse=newest_first)


def one_per_pull_request(rows: list[Row]) -> list[Row]:
    """Keep only the first row for each pull request.

    This is a list of what wants attention, not a history, so three comments on one pull request are one thing to
    look at and not three. Given rows already in order, the one kept is the most recent.

    :param rows: the rows to thin out, in the order they should be considered
    """
    kept, seen = [], set()
    for row in rows:
        identity = row.url or f"{row.repo}{row.number}"
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(row)
    return kept


def rows_to_show(count: int) -> list[Row]:
    """Return the lines to list: what changed since the user last looked, plus what is waiting on them.

    Standing state is included, so the window is useful even on a quiet day and never disagrees with the hover
    summary about whether anything wants attention. The whole list is then ordered newest first, so the most recent
    thing is at the top wherever it came from, and thinned to one row per pull request.

    :param count: how many rows to return at most
    """
    marker = last_seen()
    since = moment(marker) if marker else None
    marks = seen_marks()
    # The log is read deeply rather than to the row count, since several entries can collapse into one row.
    changes = [
        row_from_event(event, has_been_seen(event_identity(event), event["at"], marks, since))
        for event in recent_events(count * ROWS_READ_DEEPLY)
    ]
    entries, _damaged = read_snapshot()
    changes = [filled_in(row, entries or {}) for row in changes]
    listed = {row.url for row in changes if row.url}
    rows = one_per_pull_request(sorted_rows(changes + rows_from_snapshot(entries or {}, listed, marks)))[:count]
    return with_status(rows, states_by_page(entries or {}))


def filled_in(row: Row, entries: dict) -> Row:
    """Return a row with its author and hat filled in from the last poll's records, where it arrived without them.

    Only rows recorded before those fields were kept need this. The page a row leads to is the join, so a thread on
    something no longer polled stays blank until it ages out.

    :param row: the row as the log produced it
    :param entries: pull requests as the last poll recorded them
    """
    if (row.author and row.role) or not row.url:
        return row
    entry = next((candidate for candidate in entries.values() if candidate.get("url") == row.url), None)
    if entry is None:
        return row
    owner = row.author or str(entry.get("author", ""))
    hat = row.role or ("author" if entry.get("side") == "authored" else "reviewer")
    return replace(row, author=owner, role=hat)


# What the Status column may say, and the name of the ink each word is drawn in. The colours follow GitHub's own:
# green while open, violet once merged, red when closed unmerged, and the quiet ink for a draft.
STATUS_COLOURS: dict[str, str] = {
    "open": "green",
    "draft": QUIET,
    "ready": "green",
    "conflict": "pink",
    "merged": "violet",
    "closed": "red",
}

# The statuses meaning a pull request is finished, which the window hides until asked to show them.
CLOSED_STATUSES = frozenset({"merged", "closed"})

# How much of its status colour is mixed into a finished row's background, so it reads as done before a word of it
# is. A wash rather than the colour itself, which would drown every ink drawn on top of it.
CLOSED_TINT = 0.14


def pull_request_status(entry: dict | None) -> str:
    """Return the one word the Status column says about a pull request, or nothing when its state is unknown.

    Merged and closed outrank everything, since nothing else about a finished pull request matters. Among the open
    ones, a draft is a draft whatever its checks say, a conflict blocks a merge however approved it is, and ready
    means it could be merged exactly as it stands.

    :param entry: the pull request as the last poll recorded it, or None when it is no longer polled
    """
    if entry is None:
        return ""
    state = str(entry.get("state", "OPEN"))
    if state != "OPEN":
        return state.lower()
    if entry.get("isDraft"):
        return "draft"
    if entry.get("mergeable") == "CONFLICTING":
        return "conflict"
    if mergeable_now(entry):
        return "ready"
    return "open"


def states_by_page(entries: dict) -> dict[str, dict]:
    """Index the last poll's records by the page each leads to, so a row can look its pull request up.

    A pull request that has just closed is briefly recorded twice, once as it last stood open and once as closed,
    and the closed record is the one that tells the truth about it now.

    :param entries: pull requests as the last poll recorded them
    """
    indexed: dict[str, dict] = {}
    for entry in entries.values():
        url = str(entry.get("url", ""))
        if not url:
            continue
        standing = indexed.get(url)
        if standing is None or str(standing.get("state", "OPEN")) == "OPEN":
            indexed[url] = entry
    return indexed


def with_status(rows: list[Row], indexed: dict[str, dict]) -> list[Row]:
    """Return rows with the Status column filled in from the last poll's records.

    A row about something no longer polled keeps an empty status, which reads as nothing rather than as a guess.

    :param rows: the rows to fill in
    :param indexed: the records by page, as :func:`states_by_page` returns them
    """
    return [replace(row, status=pull_request_status(indexed.get(row.url))) if row.url else row for row in rows]


def closed_matches(row: Row, show_closed: bool) -> bool:
    """Return whether a row passes the closed filter.

    A row whose status is unknown always passes: hiding it would silently lose something that may well still be
    open.

    :param row: the row to judge
    :param show_closed: whether rows about finished pull requests are wanted
    """
    return show_closed or row.status not in CLOSED_STATUSES


def row_background(row: Row, inks: Palette, ground: str) -> str | None:
    """Return the background a row is drawn on, or None for the table's own.

    Only a finished pull request gets one: a wash of its status colour mixed into the table's own ground, so what is
    done reads as done at a glance even among open rows.

    :param row: the row to judge
    :param inks: the palette of the theme being drawn in
    :param ground: the colour the table is painted in, which the wash is mixed into
    """
    if row.status not in CLOSED_STATUSES:
        return None
    return blend(ink(inks, STATUS_COLOURS[row.status]), ground, CLOSED_TINT)


# The quick filters along the bottom of the window: what each is called, and which of the user's hats it keeps.
FILTER_CHOICES: tuple[tuple[str, str], ...] = (
    ("all", "All"),
    ("author", "Author"),
    ("reviewer", "Reviewer"),
    ("mention", "Mentioned"),
)


def role_matches(row: Row, wanted: str) -> bool:
    """Return whether a row belongs under a quick filter.

    :param row: the row to judge
    :param wanted: the filter's name, from :data:`FILTER_CHOICES`
    """
    return wanted == "all" or row.role == wanted


def matches_search(row: Row, needle: str) -> bool:
    """Return whether a row has some text somewhere in its columns.

    Case is ignored, as is where in a column the text falls, so "widg" finds acme/widget and "sam" finds SamRWest.

    :param row: the row to judge
    :param needle: the text looked for; none at all matches every row
    """
    if not needle:
        return True
    haystack = " ".join((row.label, row.repo, row.number, row.status, row.title, row.author, row.who, row.when))
    return needle.casefold() in haystack.casefold()


def remember_row_seen(row: Row, seen: bool) -> None:
    """Record that the user has marked a row seen or unseen, so the window and the tray icon agree about it.

    :param row: the row that was clicked
    :param seen: whether the user has now seen it
    """
    remember_seen(row_identity(row.url, row.repo, row.number), row.at, seen)
