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

import tkinter as tk
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from tkinter import font as tkfont
from tkinter import ttk

from . import APP_NAME
from .config import load_config
from .environment import make_dpi_aware
from .events import BROKEN_CI, age_in_words, is_urgent, label_for, last_seen, mergeable_now, moment, recent_events
from .snapshot import read_snapshot

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

BACKGROUND = "#0d1117"
BORDER = "#30363d"
HEADING = "#e6edf3"
TEXT = "#f0f6fc"
MUTED = "#9198a1"
LINK = "#79c0ff"
URGENT = "#ff7b72"
ROUTINE = "#e3b341"
GOOD = "#3fb950"
HOVER = "#21262d"

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


def rows_to_show(count: int) -> list[Row]:
    """Return the lines to list: what changed since the user last looked, plus what is waiting on them.

    Standing state is included, so the window is useful even on a quiet day and never disagrees with the hover
    summary about whether anything wants attention. The whole list is then ordered newest first, so the most recent
    thing is at the top wherever it came from.

    :param count: how many rows to return at most
    """
    marker = last_seen()
    since = moment(marker) if marker else None
    changes = [row_from_event(event, since is None or moment(event["at"]) > since) for event in recent_events(count)]
    listed = {row.url for row in changes if row.url}
    entries, _damaged = read_snapshot()
    return sorted_rows(changes + rows_from_snapshot(entries or {}, listed))[:count]


class Popup:
    """The frameless window itself."""

    def __init__(self, entries: list[Row]) -> None:
        """:param entries: the lines to list, in the order they should appear."""
        make_dpi_aware()
        self.entries = entries
        self.urls: dict[str, str] = {}
        self.tags: set[str] = set()
        self.sort_column = DEFAULT_SORT
        self.newest_first = True
        self.drag_origin: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.window_origin: tuple[int, int] = (0, 0)
        self.root = tk.Tk()
        self.root.withdraw()
        # Points become the right physical size only once Tk knows the real resolution of the screen.
        self.root.tk.call("tk", "scaling", self.root.winfo_fpixels("1i") / POINTS_PER_INCH)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(background=BORDER)
        self.body = tk.Frame(self.root, background=BACKGROUND)
        self.body.pack(padx=1, pady=1, fill="both", expand=True)
        self.regular = tkfont.nametofont("TkDefaultFont").copy()
        self.regular.configure(size=FONT_SIZE)
        self.bold = self.regular.copy()
        self.bold.configure(weight="bold")
        self.build()

    def characters(self, count: int) -> int:
        """Return how wide a number of characters is in this window's font, which is what column widths are given in."""
        return self.regular.measure("0") * count

    def row_height(self) -> int:
        """Return how tall one row of the table is."""
        return self.regular.metrics("linespace") + ROW_PADDING

    def build(self) -> None:
        """Lay out the heading strip, the table and the closing hint."""
        wanting = sum(1 for entry in self.entries if entry.colour != MUTED)
        self.heading_strip(f"{APP_NAME} - {wanting} wanting attention" if wanting else f"{APP_NAME} - nothing to do")
        if self.entries:
            self.table()
        else:
            tk.Label(
                self.body,
                text="Nothing is waiting on you, and nothing has changed since you last looked.",
                background=BACKGROUND,
                foreground=MUTED,
                font=self.regular,
                anchor="w",
                padx=12,
                pady=14,
            ).pack(fill="x")
        self.footer()

    def heading_strip(self, text: str) -> None:
        """Draw the title strip, which names the window and is what it is dragged by."""
        strip = tk.Frame(self.body, background=BACKGROUND, cursor="fleur")
        strip.pack(fill="x", padx=12, pady=(8, 6))
        name = tk.Label(strip, text=text, background=BACKGROUND, foreground=HEADING, font=self.bold)
        name.pack(side="left")
        close = tk.Label(strip, text="X", background=BACKGROUND, foreground=MUTED, font=self.bold, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: self.close())
        for widget in (strip, name):
            widget.bind("<Button-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.move_window)

    def style_table(self) -> None:
        """Colour the table to match the rest of the window.

        The theme is switched to one that honours background colours. The default theme on Windows draws its own,
        and would leave the table pale against everything around it.
        """
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            TABLE_STYLE,
            background=BACKGROUND,
            fieldbackground=BACKGROUND,
            foreground=TEXT,
            font=self.regular,
            rowheight=self.row_height(),
            borderwidth=0,
        )
        style.configure(f"{TABLE_STYLE}.Heading", background=BORDER, foreground=HEADING, font=self.bold, relief="flat", padding=4)
        style.map(f"{TABLE_STYLE}.Heading", background=[("active", HOVER)])
        style.map(TABLE_STYLE, background=[("selected", HOVER)], foreground=[("selected", TEXT)])

    def table(self) -> None:
        """Draw the rows as a table whose columns resize by dragging the dividers in its headings."""
        self.style_table()
        frame = tk.Frame(self.body, background=BACKGROUND)
        frame.pack(fill="both", expand=True, padx=6)
        names = [key for key, *_rest in COLUMNS]
        self.tree = ttk.Treeview(frame, columns=names, show="headings", style=TABLE_STYLE, selectmode="browse")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for key, heading, width, stretches in COLUMNS:
            self.tree.heading(key, text=heading, anchor="w", command=lambda column=key: self.sort_by(column))
            self.tree.column(key, width=self.characters(width), minwidth=self.characters(MINIMUM_COLUMN), stretch=stretches, anchor="w")
        self.fill()
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.configure(height=min(len(self.entries), self.root.winfo_screenheight() // (2 * self.row_height())))

    def tag_for(self, entry: Row) -> str:
        """Return the tag that colours one row, making it if this combination has not been seen yet.

        A row's hue says what it is and how strongly it is drawn says how stale it is, so there is one tag per pair
        of the two rather than one per state.

        :param entry: the row to colour
        """
        fade = fade_for(entry.at)
        tag = f"{entry.colour}-{fade}"
        if tag not in self.tags:
            self.tree.tag_configure(tag, foreground=blend(entry.colour, BACKGROUND, fade))
            self.tags.add(tag)
        return tag

    def fill(self) -> None:
        """Put the rows into the table in their current order, replacing whatever was there."""
        self.tree.delete(*self.tree.get_children())
        self.urls.clear()
        for entry in self.entries:
            values = (entry.label, entry.repo, entry.number, entry.title, entry.who, entry.when)
            item = self.tree.insert("", "end", values=values, tags=(self.tag_for(entry),))
            self.urls[item] = entry.url

    def sort_by(self, column: str) -> None:
        """Reorder the table by a column, turning the order around when it is already the one being sorted by.

        :param column: the column whose heading was clicked
        """
        # Dates read most usefully newest first, everything else A to Z, so each column starts the way it is wanted.
        self.newest_first = not self.newest_first if column == self.sort_column else column == DEFAULT_SORT
        self.sort_column = column
        self.entries = sorted_rows(self.entries, column, self.newest_first)
        self.fill()
        for key, heading, _width, _stretches in COLUMNS:
            marker = (" v" if self.newest_first else " ^") if key == column else ""
            self.tree.heading(key, text=f"{heading}{marker}")

    def on_click(self, event: tk.Event) -> None:
        """Open the row that was clicked, unless the click was on a heading or a divider between columns.

        :param event: the click
        """
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        url = self.urls.get(self.tree.identify_row(event.y), "")
        if url:
            self.open(url)

    def footer(self) -> None:
        """Draw the closing hint."""
        strip = tk.Frame(self.body, background=BACKGROUND)
        strip.pack(fill="x", side="bottom")
        tk.Label(
            strip,
            text="Click a row to open it. Drag a heading divider to resize a column, the title to move, any edge to resize.",
            background=BACKGROUND,
            foreground=MUTED,
            font=self.regular,
            anchor="w",
            padx=12,
            pady=6,
        ).pack(side="left")

    def edge_handles(self) -> None:
        """Put a grab strip along every edge and corner, so the window resizes from wherever the pointer lands."""
        for _name, edges, cursor, place in EDGE_HANDLES:
            handle = tk.Frame(self.root, background=BORDER, cursor=cursor)
            handle.place(**place)
            handle.bind("<Button-1>", self.start_drag)
            handle.bind("<B1-Motion>", lambda event, moving=edges: self.resize_edges(event, moving))
            handle.lift()

    def start_drag(self, event: tk.Event) -> None:
        """Remember where a drag began, and how big and where the window was when it did."""
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())
        self.window_origin = (self.root.winfo_x(), self.root.winfo_y())

    def move_window(self, event: tk.Event) -> None:
        """Move the window by however far the pointer has travelled since the drag began."""
        start_x, start_y, _width, _height = self.drag_origin
        left, top = self.window_origin
        self.root.geometry(f"+{left + event.x_root - start_x}+{top + event.y_root - start_y}")

    def resize_edges(self, event: tk.Event, edges: tuple[bool, bool, bool, bool]) -> None:
        """Resize the window by dragging one of its edges or corners.

        :param event: the motion that is dragging
        :param edges: which of the left, top, right and bottom edges this handle moves
        """
        drag_left, drag_top, drag_right, drag_bottom = edges
        start_x, start_y, width, height = self.drag_origin
        origin_left, origin_top = self.window_origin
        moved_x, moved_y = event.x_root - start_x, event.y_root - start_y

        new_width = max(MINIMUM_WIDTH, width + (moved_x if drag_right else -moved_x if drag_left else 0))
        new_height = max(MINIMUM_HEIGHT, height + (moved_y if drag_bottom else -moved_y if drag_top else 0))
        # Dragging a left or top edge keeps the far edge still, so the window's corner moves by however much the
        # size actually changed, which is not the pointer's travel once a minimum has been reached.
        new_left = origin_left + (width - new_width) if drag_left else origin_left
        new_top = origin_top + (height - new_height) if drag_top else origin_top
        self.root.geometry(f"{new_width}x{new_height}+{int(new_left)}+{int(new_top)}")

    def open(self, url: str) -> None:
        """Open a change on GitHub and close the window.

        :param url: the page to open
        """
        webbrowser.open(url)
        self.close()

    def close(self) -> None:
        """Take the window down."""
        self.root.destroy()

    def preferred_width(self) -> int:
        """Return the width every column needs at its stated size, so nothing starts out cut off.

        Capped at a share of the screen, because a window wide enough for the longest repository name anyone owns
        would otherwise stop being a popup. The columns can be dragged narrower or the window wider from there.
        """
        wanted = sum(self.characters(width) for _key, _heading, width, _stretches in COLUMNS) + WIDTH_ALLOWANCE
        return min(wanted, int(self.root.winfo_screenwidth() * WIDEST_SHARE_OF_SCREEN))

    def place(self) -> None:
        """Put the window near the pointer, sized to its contents and kept fully on screen."""
        self.root.update_idletasks()
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(max(MINIMUM_WIDTH, self.preferred_width()), screen_width - 2 * EDGE_MARGIN)
        height = min(self.root.winfo_reqheight(), screen_height - 2 * EDGE_MARGIN)
        left = min(max(EDGE_MARGIN, self.root.winfo_pointerx() - width + POINTER_OFFSET), screen_width - width - EDGE_MARGIN)
        top = min(max(EDGE_MARGIN, self.root.winfo_pointery() - height - POINTER_OFFSET), screen_height - height - EDGE_MARGIN)
        self.root.geometry(f"{width}x{height}+{int(left)}+{int(top)}")

    def show(self) -> None:
        """Display the window and wait until it is dismissed."""
        self.edge_handles()
        self.place()
        self.root.deiconify()
        self.root.focus_force()
        self.root.bind("<Escape>", lambda _event: self.close())
        # Binding this straight away can close the window before it has finished taking focus.
        self.root.after(300, lambda: self.root.bind("<FocusOut>", lambda _event: self.close()))
        self.root.mainloop()


def show_popup() -> None:
    """Show what wants attention in a frameless window, as many rows as the settings ask for."""
    Popup(rows_to_show(load_config()["popup_rows"])).show()
