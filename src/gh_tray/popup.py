"""A small frameless window listing what wants the user's attention, opened by a click on the tray icon.

It runs as its own process, like the settings window, so its user interface loop never shares a thread with the tray
icon's. That also keeps it working on macOS, where a window may only be built on a process's main thread.

The list is what changed since the user last looked, then what is merely waiting on them. Changes alone would leave
the window saying "nothing" on a quiet day while three reviews sat in the queue, which is the opposite of useful.

Having no frame means the window has none of the things a frame normally provides, so each is supplied here: it is
moved by dragging its heading strip, resized from any edge or corner, and closed by Escape, the close mark, or
clicking anything else on screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .config import LOCK_PATH, REFRESH_REQUEST_PATH, SNAPSHOT_PATH
from .environment import SingleInstance
from .events import BROKEN_CI, age_in_words, is_urgent, label_for, last_seen, mergeable_now, moment, recent_events, utc_now
from .snapshot import read_snapshot
from .storage import write_text_atomic
from .theme import PALETTE

EDGE_MARGIN = 12
# The window is opened by a click, so it appears by the pointer. Nudging it up and left keeps it clear of the
# pointer itself and, when the click was on a tray icon, clear of the taskbar.
POINTER_OFFSET = 16
MINIMUM_WIDTH = 480
MINIMUM_HEIGHT = 140

FONT_SIZE = 11
POINTS_PER_INCH = 72.0
ROW_PADDING = 8
# Room for the window's border, the scrollbar and the table's own padding, so no column starts out cut off.
WIDTH_ALLOWANCE = 60
MINIMUM_COLUMN = 4
# A window wide enough for the longest repository name anyone owns would stop being a popup, so it is capped here.
WIDEST_SHARE_OF_SCREEN = 0.62
EDGE_HANDLE_WIDTH = 6
CORNER_HANDLE_SIZE = 14
TABLE_STYLE = "ghtray.Treeview"

BACKGROUND = PALETTE.background
BORDER = PALETTE.border
HEADING = PALETTE.heading
TEXT = PALETTE.text
MUTED = PALETTE.muted
LINK = PALETTE.link
URGENT = PALETTE.urgent
ROUTINE = PALETTE.routine
GOOD = PALETTE.good
HOVER = PALETTE.hover

# How far a row keeps its colour as it ages, from untouched today to long forgotten. The table can only colour a
# whole row, not one cell of it, so age is carried by fading the row rather than by tinting its date: what a row is
# stays in the hue, and how stale it is shows in how strongly that hue is drawn.
AGE_FADE: tuple[tuple[float, float], ...] = (
    (1, 1.0),
    (7, 0.88),
    (30, 0.76),
    (90, 0.64),
    (180, 0.52),
    (float("inf"), 0.42),
)

# The colour a change is drawn in, where its kind alone decides. Anything not named here is red when it blocks
# somebody and amber otherwise.
KIND_COLOURS = {"ready_to_merge": GOOD}

# Each column: the name it is known by, its heading, how many characters wide it starts, and whether it takes the
# space a wider window adds. Every one can be resized afterwards by dragging the divider in its heading.
COLUMNS: tuple[tuple[str, str, int, bool], ...] = (
    ("change", "Change", 19, False),
    ("repo", "Repository", 46, False),
    ("pr", "PR", 7, False),
    ("title", "Title", 44, True),
    ("who", "Who", 18, False),
    ("when", "When", 10, False),
)
STRETCHING_COLUMN = "Title"
DEFAULT_SORT = "when"

# How each column sorts when its heading is clicked. Dates sort as moments and numbers as numbers, because sorting
# either as the text shown would put "3m ago" beside "3w ago" and "#7" after "#128".
SORT_KEYS = {
    "change": lambda row: row.label.casefold(),
    "repo": lambda row: row.repo.casefold(),
    "pr": lambda row: pull_request_number(row.number),
    "title": lambda row: row.title.casefold(),
    "who": lambda row: (not row.who, row.who.casefold()),
    "when": lambda row: moment(row.at),
}

# Every edge and corner the window can be dragged by: which of the left, top, right and bottom edges it moves, the
# pointer shape that says so, and where to put it. Corners come after edges so they sit on top where the two meet.
EDGE_HANDLES: tuple[tuple[str, tuple[bool, bool, bool, bool], str, dict], ...] = (
    ("left", (True, False, False, False), "sb_h_double_arrow", {"relx": 0.0, "rely": 0.0, "relheight": 1.0, "width": EDGE_HANDLE_WIDTH}),
    (
        "right",
        (False, False, True, False),
        "sb_h_double_arrow",
        {"relx": 1.0, "rely": 0.0, "anchor": "ne", "relheight": 1.0, "width": EDGE_HANDLE_WIDTH},
    ),
    ("top", (False, True, False, False), "sb_v_double_arrow", {"relx": 0.0, "rely": 0.0, "relwidth": 1.0, "height": EDGE_HANDLE_WIDTH}),
    (
        "bottom",
        (False, False, False, True),
        "sb_v_double_arrow",
        {"relx": 0.0, "rely": 1.0, "anchor": "sw", "relwidth": 1.0, "height": EDGE_HANDLE_WIDTH},
    ),
    ("top left", (True, True, False, False), "size_nw_se", {"relx": 0.0, "rely": 0.0, "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE}),
    (
        "top right",
        (False, True, True, False),
        "size_ne_sw",
        {"relx": 1.0, "rely": 0.0, "anchor": "ne", "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE},
    ),
    (
        "bottom left",
        (True, False, False, True),
        "size_ne_sw",
        {"relx": 0.0, "rely": 1.0, "anchor": "sw", "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE},
    ),
    (
        "bottom right",
        (False, False, True, True),
        "size_nw_se",
        {"relx": 1.0, "rely": 1.0, "anchor": "se", "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE},
    ),
)

TITLE_LIMIT = 90
# How many log entries to read per row shown, since several entries about one pull request collapse into one row.
ROWS_READ_DEEPLY = 5
# How long to keep re-reading after asking for a poll, and how often, while waiting for the tray to finish one.
RELOAD_ATTEMPTS = 20
RELOAD_EVERY_MS = 1000


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
    colour: str
    at: str = ""


# What each standing state is called, whose name goes beside it, whether it blocks somebody, and the colour it is
# drawn in. These describe how a pull request is right now, unlike the event labels, which describe what just
# happened. Something ready to merge is good news rather than a warning, so it is green.
STANDING_STATES: tuple[tuple[str, str, str, bool, str], ...] = (
    ("reviewing", "Awaiting your review", "author", True, URGENT),
    ("changes_requested", "Changes requested", "lastReviewBy", True, URGENT),
    ("checks_failing", "Checks failing", "lastCommitBy", True, URGENT),
    ("ready_to_merge", "Ready to merge", "lastReviewBy", False, GOOD),
)


def pull_request_number(shown: str) -> int:
    """Return a pull request number as a number, so a column of them sorts by size rather than by spelling.

    :param shown: the number as the table shows it, such as ``#128``
    """
    digits = shown.lstrip("#").strip()
    return int(digits) if digits.isdigit() else 0


def blend(colour: str, towards: str, weight: float) -> str:
    """Mix one colour towards another.

    :param colour: the colour to start from
    :param towards: the colour to move it towards
    :param weight: how much of the first to keep, where one keeps it entirely and zero loses it
    :return: the mixed colour
    """
    start = (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
    end = (int(towards[1:3], 16), int(towards[3:5], 16), int(towards[5:7], 16))
    mixed = (round(first * weight + second * (1 - weight)) for first, second in zip(start, end, strict=True))
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def fade_for(stamp: str, now: datetime | None = None) -> float:
    """Return how strongly a row of a given age should be drawn.

    :param stamp: when the row last had anything happen to it
    :param now: the moment to measure against, defaulting to the present
    """
    if not stamp:
        return AGE_FADE[0][1]
    days = max(0.0, ((now or datetime.now(UTC)) - moment(stamp)).total_seconds()) / 86400
    return next(weight for limit, weight in AGE_FADE if days < limit)


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
    return repo, f"#{number}" if number else ""


def dot_colour(event: dict, unread: bool) -> str:
    """Return the colour a change should be drawn in.

    :param event: the change the row describes
    :param unread: whether it arrived since the user last looked
    """
    if not unread:
        return MUTED
    return KIND_COLOURS.get(event["kind"], URGENT if is_urgent(event["kind"]) else ROUTINE)


def row_from_event(event: dict, unread: bool) -> Row:
    """Build a row describing something that happened.

    :param event: the change, as recorded in the log
    :param unread: whether it arrived since the user last looked
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
        colour=dot_colour(event, unread),
        at=str(event.get("at", "")),
    )


def standing_state(entry: dict) -> tuple[str, str, bool, str] | None:
    """Return how a pull request stands, when that is something worth acting on.

    :param entry: one pull request as the last poll recorded it
    :return: its label, whose name to show, whether it blocks and its colour, or None when nothing is wanted
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


def rows_from_snapshot(entries: dict, already_listed: set[str]) -> list[Row]:
    """Build rows for the pull requests that want something from the user right now.

    These fill the window when little has changed lately, so it never says "nothing" while a review is waiting.

    :param entries: pull requests as the last poll recorded them
    :param already_listed: addresses of pull requests a change has already put in the list
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


def snapshot_changed_at() -> float:
    """Return when the stored pull request state last changed, or zero when there is none.

    Used to notice that a poll has finished, since the window has no other way of hearing about one.
    """
    return SNAPSHOT_PATH.stat().st_mtime if SNAPSHOT_PATH.exists() else 0.0


def request_refresh() -> bool:
    """Leave the tray a note asking it to poll now.

    :return: whether anybody is running to read it
    """
    if SingleInstance(LOCK_PATH).acquire():
        # The lock was free, so no tray is running and the note would sit there unread.
        return False
    write_text_atomic(REFRESH_REQUEST_PATH, utc_now())
    return True


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
    # The log is read deeply rather than to the row count, since several entries can collapse into one row.
    changes = [row_from_event(event, since is None or moment(event["at"]) > since) for event in recent_events(count * ROWS_READ_DEEPLY)]
    listed = {row.url for row in changes if row.url}
    entries, _damaged = read_snapshot()
    return one_per_pull_request(sorted_rows(changes + rows_from_snapshot(entries or {}, listed)))[:count]


