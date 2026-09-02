"""The frameless window the click-through list is drawn in.

The table is a spreadsheet widget rather than the toolkit's own, because that one colours a whole row at a time and
here each cell wants its own: a row says what it is in one colour and how stale it is in another, and neither
should have to give way to the other.

Clicking a row opens it on GitHub. Right-clicking marks it seen, and right-clicking again marks it unseen.

The window is built once and then hidden rather than closed, and shows itself again whenever the tray leaves a note
asking it to. Building it costs most of a second, which is a long time to wait after clicking a tray icon.

Having no frame means the window has none of the things a frame normally provides, so each is supplied here: it is
moved by dragging its heading strip, resized from any edge or corner, and closed by Escape, the close mark, or
clicking anything else on screen.
"""

from __future__ import annotations

import time
import tkinter as tk
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from tkinter import font as tkfont

from loguru import logger
from tksheet import Sheet

from . import APP_NAME
from .config import POPUP_LOCK_PATH, POPUP_REQUEST_PATH, load_config
from .environment import SingleInstance, make_dpi_aware, pointer_scaling, work_area
from .popup import (
    COLUMNS,
    DEFAULT_SORT,
    FILTER_CHOICES,
    GLYPHS,
    RELOAD_ATTEMPTS,
    RELOAD_EVERY_MS,
    SEEN_STRENGTH,
    STATUS_COLOURS,
    Row,
    age_colour,
    closed_matches,
    glyph_for,
    org_and_name,
    remember_column_widths,
    remember_row_seen,
    remember_width,
    remembered_column_widths,
    remembered_width,
    request_refresh,
    requested_spot,
    role_matches,
    row_background,
    rows_to_show,
    snapshot_changed_at,
    sorted_rows,
    start_window,
    who_colour,
)
from .theme import PALETTE, blend, chosen_style, palette

EDGE_MARGIN = 12
# The window is opened by a click, so it appears by the pointer. Nudging it up and left keeps it clear of the
# pointer itself and, when the click was on a tray icon, clear of the taskbar.
POINTER_OFFSET = 16
# How far above the pointer the window sits, so a click near the bottom of the screen still leaves it clear.
POINTER_GAP = 24
MINIMUM_WIDTH = 480
MINIMUM_HEIGHT = 140
# Never in any font, so its width is whatever this one draws for a character it does not have.
MISSING_GLYPH = "￿"
FONT_SIZE = 11
POINTS_PER_INCH = 72.0
ROW_PADDING = 10
# Room for the window's border, the scrollbar and the table's own padding, so no column starts out cut off.
WIDTH_ALLOWANCE = 70
# A window wide enough for the longest repository name anyone owns would stop being a popup, so it is capped here.
WIDEST_SHARE_OF_SCREEN = 0.62
EDGE_HANDLE_WIDTH = 6
CORNER_HANDLE_SIZE = 14
# What the table adds around its rows, and the most of the screen the window may take up.
TABLE_TRIM = 8
TALLEST_SHARE_OF_SCREEN = 0.55

# The date, the status and the two name columns are each drawn on a scale of their own, so they need finding among
# the columns.
DATE_COLUMN = "when"
WHO_COLUMN = "who"
AUTHOR_COLUMN = "author"
STATUS_COLUMN = "status"

# What the table widget understands as a right click. It adds the other button macOS uses for one itself, so this
# is the same everywhere.
RIGHT_CLICK = "<3>"

# How often the hidden window looks for a note asking it to show itself. Ten times a second costs nothing and is
# faster than anyone can notice, so a click on the tray icon reads as immediate.
WATCH_EVERY_MS = 100
# How long the window takes to finish coming up and settle on the focus. Until then a loss of focus is part of it
# arriving rather than the user clicking elsewhere, and dismissing on that would mean it never appeared at all.
FOCUS_SETTLE_MS = 300
# How soon after the window loses focus a note asking for it counts as the same click. The tray waits half a
# second to see whether a second click follows, so its note arrives that much after the click that sent it.
TOGGLE_WITHIN_SECONDS = 1.2

# Every edge and corner the window can be dragged by: which of the left, top, right and bottom edges it moves, the
# pointer shape that says so, and where to put it. Corners come after edges so they sit on top where the two meet.
# The pointer shapes are the names every platform knows. The ones Windows adds for the diagonals do not exist on
# a Linux desktop, where naming one is not ignored but refused, and takes the whole window with it.
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
    (
        "top left",
        (True, True, False, False),
        "top_left_corner",
        {"relx": 0.0, "rely": 0.0, "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE},
    ),
    (
        "top right",
        (False, True, True, False),
        "top_right_corner",
        {"relx": 1.0, "rely": 0.0, "anchor": "ne", "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE},
    ),
    (
        "bottom left",
        (True, False, False, True),
        "bottom_left_corner",
        {"relx": 0.0, "rely": 1.0, "anchor": "sw", "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE},
    ),
    (
        "bottom right",
        (False, False, True, True),
        "bottom_right_corner",
        {"relx": 1.0, "rely": 1.0, "anchor": "se", "width": CORNER_HANDLE_SIZE, "height": CORNER_HANDLE_SIZE},
    ),
)


def column_of(key: str) -> int:
    """Return where a named column sits in the table.

    :param key: the name a column is known by
    """
    return next(index for index, (name, *_rest) in enumerate(COLUMNS) if name == key)


def cells_of(entry: Row, glyphs: bool) -> list[str]:
    """Return one row's text, in column order.

    :param entry: the row to lay out
    :param glyphs: whether the window can draw the marks that head a row
    """
    label = f"{glyph_for(entry)}  {entry.label}" if glyphs else entry.label
    owner, name = org_and_name(entry.repo)
    return [label, owner, name, entry.number, entry.status, entry.title, entry.author, entry.who, entry.when]


def can_draw_glyphs(font: tkfont.Font) -> bool:
    """Return whether this window's font has the marks that head a row.

    A font without them draws a box for each, which says less than nothing. There is no way to ask a font what it
    holds, so each mark is measured against a character no font has: one that comes out the same width is being
    drawn as that same empty box.

    :param font: the font the table is drawn in
    """
    missing = font.measure(MISSING_GLYPH)
    return all(font.measure(glyph) not in (0, missing) for glyph in GLYPHS)


class Popup:
    """The frameless window itself."""

    def __init__(self, entries: list[Row]) -> None:
        """:param entries: the lines to list, in the order they should appear."""
        make_dpi_aware()
        # Everything the log offered, and the part of it the chosen quick filter lets through, which is what the
        # table shows. Marks are written to both, so switching filters does not forget them.
        self.all_entries = list(entries)
        self.role_filter = "all"
        # Rows about closed pull requests start hidden: they are done, and the window is a list of what is not.
        self.show_closed = False
        self.sort_column = DEFAULT_SORT
        self.newest_first = True
        self.apply_filter()
        self.drag_origin: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.window_origin: tuple[int, int] = (0, 0)
        # Whether the window is up and has settled, which is what tells a click elsewhere from the window's own
        # arrival taking the focus, and when a click elsewhere last put it away. No sentinel number for the
        # second: the reference point of a monotonic clock is undefined, so any number chosen to mean "never"
        # could legitimately occur.
        self.showing = False
        self.dismissed_at: float | None = None
        # Where the click that last asked for the window was, which is where it comes up.
        self.spot: tuple[int, int] | None = None
        # The pending change of mind about the window having arrived, cancelled if it goes away first.
        self.settling: str | None = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(background=PALETTE.border)
        self.body = tk.Frame(self.root, background=PALETTE.background)
        self.body.pack(padx=1, pady=1, fill="both", expand=True)
        self.apply_scaling()
        self.prepare_fonts()
        self.build()
        # How tall a row came out, which is the whole of what the display's scaling decides for this window.
        self.built_for = self.row_height()

    def characters(self, count: int) -> int:
        """Return how wide a number of characters is in this window's font, which is what column widths are given in."""
        return self.regular.measure("0") * count

    def row_height(self) -> int:
        """Return how tall one row of the table is."""
        return self.regular.metrics("linespace") + ROW_PADDING

    def apply_scaling(self) -> None:
        """Tell the toolkit how finely the display under the pointer draws.

        The number is asked of the platform rather than taken from the toolkit's own reckoning of the screen, which
        is settled when it starts and a display that goes away and comes back can leave it behind. Where the
        platform cannot say, that reckoning is all there is.
        """
        # Kept for the remembered sizes, which are recorded against how finely the display was drawing at the time.
        self.dots = pointer_scaling() or self.root.winfo_fpixels("1i")
        self.root.tk.call("tk", "scaling", self.dots / POINTS_PER_INCH)

    def prepare_fonts(self) -> None:
        """Take fresh copies of the toolkit's own font, in the size and at the scaling this window draws in.

        Fresh copies rather than resized ones: a font already made carries the size in pixels it was made with, and
        changing the scaling underneath it does not move that. A copy taken afterwards measures itself anew, so the
        same display always gives the same sizes however many times this has been done.
        """
        self.regular = tkfont.nametofont("TkDefaultFont").copy()
        self.regular.configure(size=FONT_SIZE)
        self.bold = self.regular.copy()
        self.bold.configure(weight="bold")

    def text_height(self) -> int:
        """Return how tall a line of this window's text would be, drawn at the display's current scaling."""
        measuring = tkfont.nametofont("TkDefaultFont").copy()
        measuring.configure(size=FONT_SIZE)
        return measuring.metrics("linespace") + ROW_PADDING

    def suit_the_display(self) -> None:
        """Put the window back in step with the display, which can change while the window is hidden away.

        Column widths, row heights and the size of the window are all measured from the text once, when the window
        is built. When the display starts drawing text at another size they are all wrong together, and the only
        way back is to build the contents again.
        """
        self.apply_scaling()
        if self.text_height() == self.built_for:
            return
        logger.info("the display now wants rows {} tall rather than {}, building again", self.text_height(), self.built_for)
        for part in self.body.winfo_children():
            part.destroy()
        self.prepare_fonts()
        self.build()
        self.built_for = self.row_height()

    def heading_text(self) -> str:
        """Return the line at the top of the window, which counts the rows not yet marked seen."""
        waiting = sum(1 for entry in self.entries if not entry.seen)
        return f"{APP_NAME} - {waiting} notification{'' if waiting == 1 else 's'}" if waiting else f"{APP_NAME} - nothing to do"

    def build(self) -> None:
        """Lay out the heading strip, the table and the closing hint.

        The table is built whenever anything is on offer, even if the filters currently hide all of it, so that
        widening a filter has somewhere to put the rows it lets back in.
        """
        self.heading_strip(self.heading_text())
        if self.all_entries:
            self.table()
        else:
            tk.Label(
                self.body,
                text="Nothing is waiting on you, and nothing has changed since you last looked.",
                background=PALETTE.background,
                foreground=PALETTE.muted,
                font=self.regular,
                anchor="w",
                padx=12,
                pady=14,
            ).pack(fill="x")
        self.footer()

    def heading_strip(self, text: str) -> None:
        """Draw the title strip, which names the window and is what it is dragged by."""
        strip = tk.Frame(self.body, background=PALETTE.background, cursor="fleur")
        strip.pack(fill="x", padx=12, pady=(8, 6))
        self.name = name = tk.Label(strip, text=text, background=PALETTE.background, foreground=PALETTE.heading, font=self.bold)
        name.pack(side="left")
        self.close_mark = close = tk.Label(
            strip, text="X", background=PALETTE.background, foreground=PALETTE.muted, font=self.bold, cursor="hand2", padx=6
        )
        close.pack(side="right")
        # Brightening under the pointer, so the mark answers before it is clicked.
        close.bind("<Enter>", lambda _event: close.configure(foreground=PALETTE.heading))
        close.bind("<Leave>", lambda _event: close.configure(foreground=PALETTE.muted))
        # Bound to the method itself rather than through a lambda, so a name that no longer exists fails here while
        # the window is being built rather than silently doing nothing when the mark is clicked.
        close.bind("<Button-1>", self.hide)
        for widget in (strip, name):
            widget.bind("<Button-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.move_window)

    def table(self) -> None:
        """Draw the rows as a spreadsheet, whose columns resize by dragging the dividers in its headings."""
        self.glyphs = can_draw_glyphs(self.regular)
        self.sheet = Sheet(
            self.body,
            headers=[heading for _key, heading, _width, _stretches in COLUMNS],
            data=[cells_of(entry, self.glyphs) for entry in self.entries],
            font=(self.regular.cget("family"), FONT_SIZE, "normal"),
            header_font=(self.bold.cget("family"), FONT_SIZE, "bold"),
            header_align="w",
            align="w",
            default_row_height=self.row_height(),
            show_row_index=False,
            show_top_left=False,
            show_x_scrollbar=False,
            table_bg=PALETTE.background,
            table_fg=PALETTE.text,
            table_grid_fg=PALETTE.background,
            header_bg=PALETTE.surface,
            header_fg=PALETTE.heading,
            header_grid_fg=PALETTE.border,
            header_border_fg=PALETTE.border,
            table_selected_cells_bg=PALETTE.hover,
            table_selected_rows_bg=PALETTE.hover,
            table_selected_box_cells_fg=PALETTE.border,
            table_selected_box_rows_fg=PALETTE.border,
            outline_color=PALETTE.background,
            frame_bg=PALETTE.background,
            show_vertical_grid=False,
            show_horizontal_grid=False,
        )
        self.sheet.pack(fill="both", expand=True, padx=6)
        # Only what a reader needs: resizing a column, moving about, and copying a cell out.
        self.sheet.enable_bindings("single_select", "column_width_resize", "double_click_column_resize", "arrowkeys", "copy")
        self.sheet.set_column_widths(iter(self.column_widths()))
        self.paint()
        self.sheet.bind("<Button-1>", self.on_click, add="+")
        self.sheet.bind(RIGHT_CLICK, self.on_right_click)
        self.sheet.extra_bindings("column_width_resize", self.on_column_dragged)
        # The widget asks for a fixed minimum height of its own, however few rows it holds, which padded the window
        # with empty table on a quiet day. Its height is decided here from the rows, so its own say is switched off
        # and it takes whatever it is given.
        self.sheet.grid_propagate(False)
        self.sheet.configure(height=self.table_height())

    def column_widths(self) -> list[int]:
        """Return each column's width in pixels: the one the user last dragged it to, or its stated starting size."""
        dragged = remembered_column_widths(self.dots)
        return [dragged.get(key) or self.characters(width) for key, _heading, width, _stretches in COLUMNS]

    def on_column_dragged(self, _event: object = None) -> None:
        """Remember every column's width, now that the user has dragged one of them."""
        widths = {key: round(width) for (key, *_rest), width in zip(COLUMNS, self.sheet.get_column_widths(), strict=True)}
        remember_column_widths(widths, self.dots)

    def paint(self) -> None:
        """Colour every cell.

        A row is drawn in the colour of what it is, at full strength while it wants attention and dimmed once seen.
        That is the only thing that dims it: how old something is has a scale of its own in the date column, which
        runs from just-happened to long-forgotten and reads as a gradient down the window. Dimming for age as well
        left two rows of the same sort looking different for a reason nobody could name.

        The name has a colour of its own too, dealt to it and kept, so the same person reads as the same colour in
        every row. It dims with the rest of the row once seen, unlike the date, whose scale is the point of it.
        """
        self.sheet.dehighlight_cells(all_=True)
        date_column = column_of(DATE_COLUMN)
        for row, entry in enumerate(self.entries):
            inks = {
                date_column: age_colour(entry.at),
                column_of(WHO_COLUMN): who_colour(entry.who),
                column_of(AUTHOR_COLUMN): who_colour(entry.author),
                column_of(STATUS_COLUMN): STATUS_COLOURS.get(entry.status, PALETTE.muted),
            }
            # A finished pull request's row sits on a wash of its status colour, so it reads as done at a glance.
            wash = row_background(entry)
            for column in range(len(COLUMNS)):
                ink = inks.get(column, entry.colour)
                if entry.seen and column != date_column:
                    ink = blend(ink, PALETTE.background, SEEN_STRENGTH)
                self.sheet.highlight_cells(row=row, column=column, fg=ink, bg=wash)

    def refill(self) -> None:
        """Put the rows into the table in their current order, and colour them again."""
        self.sheet.set_sheet_data([cells_of(entry, self.glyphs) for entry in self.entries], reset_col_positions=False, redraw=False)
        self.paint()
        self.sheet.redraw()

    def on_click(self, event: tk.Event) -> None:
        """Act on a left click: a heading sorts by its column, a row opens on GitHub.

        :param event: the click
        """
        if self.sheet.identify_region(event) == "header":
            self.sort_from_heading(event)
            return
        row = self.clicked_row(event)
        if row is not None and self.entries[row].url:
            self.open(self.entries[row].url)

    def on_right_click(self, event: tk.Event) -> None:
        """Mark whichever row was right-clicked seen, or unseen when it is already marked.

        :param event: the click
        """
        row = self.clicked_row(event)
        if row is not None:
            self.set_seen(row, not self.entries[row].seen)

    def clicked_row(self, event: tk.Event) -> int | None:
        """Return which row a click landed on, or nothing when it landed anywhere else.

        :param event: the click
        """
        if self.sheet.identify_region(event) != "table":
            return None
        row = self.sheet.identify_row(event, allow_end=False)
        return row if row is not None and 0 <= row < len(self.entries) else None

    def set_seen(self, row: int, seen: bool) -> None:
        """Mark one row seen or unseen, remember it for next time, and redraw.

        :param row: which row to mark
        :param seen: whether the user has now seen it
        """
        if self.entries[row].seen == seen:
            return
        marked = replace(self.entries[row], seen=seen)
        self.entries[row] = marked
        self.all_entries = [marked if entry.url == marked.url and entry.at == marked.at else entry for entry in self.all_entries]
        remember_row_seen(marked, seen)
        self.refill()
        self.name.configure(text=self.heading_text())

    def sort_from_heading(self, event: tk.Event) -> None:
        """Sort by the column whose heading was clicked, unless the click was on a divider between two.

        :param event: the click
        """
        column = self.sheet.identify_column(event, allow_end=False)
        if self.sheet.MT.current_cursor == "sb_h_double_arrow" or column is None or column >= len(COLUMNS):
            return
        self.sort_by(COLUMNS[column][0])

    def sort_by(self, column: str) -> None:
        """Reorder the table by a column, turning the order around when it is already the one being sorted by.

        :param column: the column whose heading was clicked
        """
        # Dates read most usefully newest first, everything else A to Z, so each column starts the way it is wanted.
        self.newest_first = not self.newest_first if column == self.sort_column else column == DEFAULT_SORT
        self.sort_column = column
        self.entries = sorted_rows(self.entries, column, self.newest_first)
        self.refill()
        marker = " v" if self.newest_first else " ^"
        self.sheet.headers([f"{heading}{marker if key == column else ''}" for key, heading, _width, _stretches in COLUMNS])

    def footer(self) -> None:
        """Draw the quick filters, the closed toggle, the button that asks for a fresh look, and the closing hint."""
        self.hint = tk.Label(
            self.body,
            text="Click a row to open it, right-click to mark it seen. Click a heading to sort, drag the title to move, an edge to resize.",
            background=PALETTE.background,
            foreground=PALETTE.muted,
            font=self.regular,
            anchor="w",
            padx=12,
            pady=4,
        )
        self.hint.pack(side="bottom", fill="x")
        strip = tk.Frame(self.body, background=PALETTE.background)
        strip.pack(fill="x", side="bottom")
        self.chips: dict[str, tk.Label] = {}
        for name, label in FILTER_CHOICES:
            chosen = name == self.role_filter
            chip = tk.Label(
                strip,
                text=label,
                background=PALETTE.selection if chosen else PALETTE.surface,
                foreground=PALETTE.heading if chosen else PALETTE.muted,
                font=self.bold,
                cursor="hand2",
                padx=10,
                pady=3,
            )
            chip.pack(side="left", padx=(12 if not self.chips else 4, 0), pady=6)
            chip.bind("<Button-1>", lambda _event, wanted=name: self.choose_filter(wanted))
            self.chips[name] = chip
        # Set apart from the quick filters, since it works alongside them rather than instead of them.
        self.closed_chip = tk.Label(strip, text="Show closed", font=self.bold, cursor="hand2", padx=10, pady=3)
        self.closed_chip.pack(side="left", padx=(12, 0), pady=6)
        self.closed_chip.bind("<Button-1>", lambda _event: self.toggle_closed())
        self.style_closed_chip()
        self.refresh_button = tk.Label(
            strip,
            text="Refresh",
            background=PALETTE.selection,
            foreground=PALETTE.heading,
            font=self.bold,
            cursor="hand2",
            padx=12,
            pady=3,
        )
        self.refresh_button.pack(side="right", padx=12, pady=6)
        self.refresh_button.bind("<Button-1>", lambda _event: self.refresh())
        self.refresh_button.bind("<Enter>", lambda _event: self.refresh_button.configure(background=blend(PALETTE.selection, PALETTE.heading, 0.85)))
        self.refresh_button.bind("<Leave>", lambda _event: self.refresh_button.configure(background=PALETTE.selection))

    def refresh(self) -> None:
        """Ask for a fresh look at GitHub, and keep re-reading until it arrives.

        The tray is a separate process and the only one allowed to poll, so this leaves it a note rather than
        polling itself. With no tray running there is nobody to answer, and the window says so instead of waiting
        for something that will never come.
        """
        if not request_refresh():
            self.hint.configure(text="Nothing is polling. Start gh-tray for this to fetch anything new.")
            self.reload()
            return
        self.refresh_button.configure(text="Refreshing", foreground=PALETTE.muted)
        self.hint.configure(text="Asked for a fresh look. This will update when it arrives.")
        self.await_update(RELOAD_ATTEMPTS, snapshot_changed_at())

    def await_update(self, attempts_left: int, was: float) -> None:
        """Re-read until the stored data changes or waiting has gone on long enough.

        :param attempts_left: how many more times to look
        :param was: when the stored data last changed, as it stood before asking
        """
        if snapshot_changed_at() != was:
            self.reload()
            self.refresh_button.configure(text="Refresh", foreground=PALETTE.heading)
            self.hint.configure(text="Up to date.")
            return
        if attempts_left <= 0:
            self.refresh_button.configure(text="Refresh", foreground=PALETTE.heading)
            self.hint.configure(text="No answer yet. The tray may be busy or unable to reach GitHub.")
            return
        self.root.after(RELOAD_EVERY_MS, lambda: self.await_update(attempts_left - 1, was))

    def reload(self) -> None:
        """Read the stored data again and redraw the table in the order and filter currently chosen."""
        self.all_entries = rows_to_show(load_config()["popup_rows"])
        self.apply_filter()
        if hasattr(self, "sheet"):
            self.refill()

    def apply_filter(self) -> None:
        """Reduce everything on offer to what the chosen filters let through, in the chosen order."""
        kept = [entry for entry in self.all_entries if role_matches(entry, self.role_filter) and closed_matches(entry, self.show_closed)]
        self.entries = sorted_rows(kept, self.sort_column, self.newest_first)

    def redraw_filtered(self) -> None:
        """Redraw around whatever the filters now leave.

        The window stays where it is: it re-fits its height to the rows now shown, but a filter click must not
        teleport it back to the pointer.
        """
        self.apply_filter()
        if hasattr(self, "sheet"):
            self.refill()
        self.name.configure(text=self.heading_text())
        self.refit_height()

    def choose_filter(self, wanted: str) -> None:
        """Switch the quick filter and redraw around whatever it leaves.

        :param wanted: the filter's name, from :data:`FILTER_CHOICES`
        """
        self.role_filter = wanted
        for name, chip in self.chips.items():
            chosen = name == wanted
            chip.configure(
                background=PALETTE.selection if chosen else PALETTE.surface,
                foreground=PALETTE.heading if chosen else PALETTE.muted,
            )
        self.redraw_filtered()

    def toggle_closed(self) -> None:
        """Show or hide the rows about closed pull requests, and redraw around whatever that leaves."""
        self.show_closed = not self.show_closed
        self.style_closed_chip()
        self.redraw_filtered()

    def style_closed_chip(self) -> None:
        """Colour the closed toggle for whether the rows it governs are currently shown."""
        self.closed_chip.configure(
            background=PALETTE.selection if self.show_closed else PALETTE.surface,
            foreground=PALETTE.heading if self.show_closed else PALETTE.muted,
        )

    def refit_height(self) -> None:
        """Re-fit the window's height to the rows now shown, growing and shrinking from its top edge.

        The bottom edge stays where it is: the window opens above the click, usually just clear of the taskbar,
        so growing downward would take the new rows straight off the bottom of the screen. Growing upward keeps
        every row on it, and the top only gives way when the screen has no more room above.
        """
        if not hasattr(self, "sheet"):
            return
        bottom = self.root.winfo_y() + self.root.winfo_height()
        _width, usable_height = self.usable_screen()
        tallest = int(usable_height * TALLEST_SHARE_OF_SCREEN)
        around = self.root.winfo_reqheight() - self.sheet.winfo_reqheight()
        self.sheet.configure(height=min(self.table_height(), tallest - around))
        self.root.update_idletasks()
        height = min(max(MINIMUM_HEIGHT, self.root.winfo_reqheight()), tallest)
        top = min(max(EDGE_MARGIN, bottom - height), usable_height - height - EDGE_MARGIN)
        self.root.geometry(f"{self.root.winfo_width()}x{height}+{self.root.winfo_x()}+{int(top)}")

    def edge_handles(self) -> None:
        """Put a grab strip along every edge and corner, so the window resizes from wherever the pointer lands."""
        for _name, edges, cursor, place in EDGE_HANDLES:
            handle = tk.Frame(self.root, background=PALETTE.border, cursor=cursor)
            handle.place(**place)
            handle.bind("<Button-1>", self.start_drag)
            handle.bind("<B1-Motion>", self.dragger(edges))
            handle.bind("<ButtonRelease-1>", self.on_resize_finished)
            handle.lift()

    def on_resize_finished(self, _event: object = None) -> None:
        """Remember the width the user dragged the window to, once the drag lets go.

        On the release rather than during the drag, so a resize is one write and not hundreds. A drag that moved
        only the height says nothing worth keeping: the height follows the rows on the next showing, so keeping it
        would only ever add blank table or hide rows.
        """
        _x, _y, was_width, _was_height = self.drag_origin
        width = self.root.winfo_width()
        if width != was_width:
            remember_width(width, self.dots)

    def dragger(self, edges: tuple[bool, bool, bool, bool]) -> Callable[[tk.Event], None]:
        """Return the handler that resizes the window by one particular edge or corner.

        Built here rather than in the loop that binds it, so each handler keeps the edges it was made for instead
        of all of them sharing whichever came last.

        :param edges: which of the left, top, right and bottom edges this handle moves
        """

        def drag(event: tk.Event) -> None:
            """Resize the window by however far this handle has been dragged."""
            self.resize_edges(event, edges)

        return drag

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
        """Open a change on GitHub and put the window away.

        :param url: the page to open
        """
        webbrowser.open(url)
        self.hide()

    def hide(self, *_) -> None:
        """Put the window away, keeping it loaded so the next showing is immediate."""
        self.showing = False
        self.stop_settling()
        self.root.withdraw()

    def on_focus_out(self, *_) -> None:
        """Consider putting the window away, now that something has taken the focus from it.

        Clicking a heading or a row hands the focus to that part of the window, which the toolkit reports exactly
        as it reports the focus leaving for another application. Asking where the focus ended up tells the two
        apart, and asking once the move has finished is what makes the answer trustworthy.
        """
        if self.showing:
            self.root.after_idle(self.hide_if_the_focus_left)

    def hide_if_the_focus_left(self) -> None:
        """Put the window away if the focus has gone to another application rather than into this window."""
        if not self.showing or self.root.focus_displayof() is not None:
            return
        self.hide()
        self.dismissed_at = time.monotonic()

    def preferred_width(self) -> int:
        """Return the width every column needs at its stated size, capped so the window stays a popup."""
        wanted = sum(self.characters(width) for _key, _heading, width, _stretches in COLUMNS) + WIDTH_ALLOWANCE
        return min(wanted, int(self.usable_screen()[0] * WIDEST_SHARE_OF_SCREEN))

    def table_height(self) -> int:
        """Return how tall the table needs to be to show every row it has, without leaving empty space below."""
        # One row's worth for the headings, and a little for the border the widget draws round itself.
        return (len(self.entries) + 1) * self.row_height() + TABLE_TRIM

    def usable_screen(self) -> tuple[int, int]:
        """Return how much of the screen a window may occupy, leaving out any taskbar, dock or panel.

        The toolkit reports the largest a window can be made, which every desktop works out for itself with its own
        bars already deducted. That is the same answer on Windows, macOS and Linux without asking any of them
        directly. Where it looks wrong, the whole screen is used instead.

        :return: the usable width and height in pixels
        """
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            width, height = self.root.wm_maxsize()
        except tk.TclError:
            width, height = screen_width, screen_height
        if not (0 < width <= screen_width and 0 < height <= screen_height):
            width, height = screen_width, screen_height
        return work_area((width, height))

    def place(self) -> None:
        """Put the window by the click that asked for it, sized to its contents and clear of the desktop's own bars.

        By the click, not the pointer: the window comes up half a second after the click that asked for it, and a
        pointer already moving would otherwise drag the window off to wherever it had got to.

        The height follows how many rows there actually are, so a quiet day gets a small window rather than a tall
        one mostly full of nothing. That holds only until the user drags the window to a size of their own, which
        is then used instead, however many rows there are.
        """
        self.root.update_idletasks()
        usable_width, usable_height = self.usable_screen()
        tallest = int(usable_height * TALLEST_SHARE_OF_SCREEN)
        if hasattr(self, "sheet"):
            # The cap leaves room for everything around the table. Capping the table alone lets the whole window
            # outgrow the ceiling, and the desktop answers that by quietly unpacking whatever no longer fits,
            # which is how a window can lose its bottom strip.
            around = self.root.winfo_reqheight() - self.sheet.winfo_reqheight()
            self.sheet.configure(height=min(self.table_height(), tallest - around))
            self.root.update_idletasks()
        width, height = self.wanted_size(usable_width, usable_height, tallest)
        at_x, at_y = self.spot or (self.root.winfo_pointerx(), self.root.winfo_pointery())
        left = min(max(EDGE_MARGIN, at_x - width + POINTER_OFFSET), usable_width - width - EDGE_MARGIN)
        # The window sits above the click, and never below what the desktop says is usable, so a click low on the
        # screen does not put it behind the taskbar.
        top = min(max(EDGE_MARGIN, at_y - height - POINTER_GAP), usable_height - height - EDGE_MARGIN)
        self.root.geometry(f"{width}x{height}+{int(left)}+{int(top)}")

    def wanted_size(self, usable_width: int, usable_height: int, tallest: int) -> tuple[int, int]:
        """Return how big the window should come up.

        The width is the one the user dragged it to, where they have, kept on the screen since it may have been
        dragged out on a larger display than this one. The height always follows the rows: snug around a few, and
        no more than its ceiling over many.

        :param usable_width: how wide the screen is with the desktop's own bars left out
        :param usable_height: the same for its height
        :param tallest: the most height the window may take
        """
        dragged = remembered_width(self.dots)
        wanted = dragged if dragged else self.preferred_width()
        width = min(max(MINIMUM_WIDTH, wanted), usable_width - 2 * EDGE_MARGIN)
        height = min(max(MINIMUM_HEIGHT, self.root.winfo_reqheight()), tallest)
        return width, height

    def show(self, spot: tuple[int, int] | None = None) -> None:
        """Bring the window up beside a click, with whatever is currently waiting.

        :param spot: where on screen the click that asked was, or None to use the pointer
        """
        self.spot = spot
        self.dismissed_at = None
        self.suit_the_display()
        self.reload()
        self.place()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.stop_settling()
        self.settling = self.root.after(FOCUS_SETTLE_MS, self.settle)
        logger.debug("showing {} rows in the {} theme", len(self.entries), "dark" if PALETTE.dark else "light")

    def stop_settling(self) -> None:
        """Drop any pending decision that the window has arrived, since it is no longer arriving."""
        if self.settling is not None:
            self.root.after_cancel(self.settling)
            self.settling = None

    def settle(self) -> None:
        """Start treating a loss of focus as a dismissal, now that the window has finished coming up."""
        self.settling = None
        self.showing = True

    def serve(self, waiting: SingleInstance) -> None:
        """Stay loaded and hidden, showing the window whenever the tray asks for it.

        Building the window costs most of a second, nearly all of it loading the drawing libraries and laying the
        table out. Doing that once and then hiding rather than closing is what makes a click on the tray icon show
        something straight away.

        :param waiting: the lock saying this process is the one window waiting to be shown
        """
        self.waiting = waiting
        self.edge_handles()
        self.root.bind("<Escape>", self.hide)
        self.root.bind("<FocusOut>", self.on_focus_out)
        self.watch()
        self.root.mainloop()

    def watch(self) -> None:
        """Look for a note asking the window to show itself, and keep looking."""
        if POPUP_REQUEST_PATH.exists():
            self.answer()
        self.root.after(WATCH_EVERY_MS, self.watch)

    def answer(self) -> None:
        """Act on a note asking for the window: show it, put it away, or hand over to one drawn for how things are now."""
        if self.showing or self.just_dismissed():
            # Asking for the window while it is up means put it away. Whether the click that asked has already
            # dismissed it by taking its focus depends on the desktop, so both are treated the same.
            POPUP_REQUEST_PATH.unlink(missing_ok=True)
            self.hide()
            return
        if self.theme_changed():
            self.hand_over()
            return
        spot = requested_spot()
        POPUP_REQUEST_PATH.unlink(missing_ok=True)
        self.show(spot)

    def just_dismissed(self) -> bool:
        """Return whether losing the focus has this moment put the window away.

        Clicking the tray icon while the window is up takes the focus from it, which puts it away, and the note
        asking for it arrives a moment later. Answering that note would bring the window straight back and leave
        the click having done nothing at all.
        """
        return self.dismissed_at is not None and time.monotonic() - self.dismissed_at < TOGGLE_WITHIN_SECONDS

    def theme_changed(self) -> bool:
        """Return whether the desktop or the settings now ask for different colours than the window was drawn in.

        Sizes can be put right where the window stands, but colours cannot: they are read once, as the module
        loads, and reach into every part of it.
        """
        return palette(chosen_style()).dark != PALETTE.dark

    def hand_over(self) -> None:
        """Give way to a freshly built window, drawn for how the desktop is now.

        The note asking for a window is left where it is, so the replacement answers it as soon as it is ready and
        the click that arrived here is not lost.
        """
        logger.info("handing over to a fresh window")
        self.waiting.release()
        start_window()
        self.root.destroy()


def serve_popup() -> None:
    """Keep the changes window loaded and hidden, ready to be shown the moment the tray asks."""
    waiting = SingleInstance(POPUP_LOCK_PATH)
    if not waiting.acquire():
        logger.info("a window is already waiting, leaving it to it")
        return
    try:
        Popup(rows_to_show(load_config()["popup_rows"])).serve(waiting)
    finally:
        waiting.release()
