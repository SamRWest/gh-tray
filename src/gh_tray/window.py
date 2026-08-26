"""The frameless window the click-through list is drawn in.

The table is a spreadsheet widget rather than the toolkit's own, because that one colours a whole row at a time and
here each cell wants its own: a row says what it is in one colour and how stale it is in another, and neither
should have to give way to the other.

Clicking a row opens it on GitHub. Right-clicking marks it seen, and right-clicking again marks it unseen.

Having no frame means the window has none of the things a frame normally provides, so each is supplied here: it is
moved by dragging its heading strip, resized from any edge or corner, and closed by Escape, the close mark, or
clicking anything else on screen.
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from dataclasses import replace
from tkinter import font as tkfont

from loguru import logger
from tksheet import Sheet

from . import APP_NAME
from .config import load_config
from .environment import make_dpi_aware, work_area
from .popup import (
    COLUMNS,
    DEFAULT_SORT,
    GLYPHS,
    RELOAD_ATTEMPTS,
    RELOAD_EVERY_MS,
    SEEN_STRENGTH,
    Row,
    age_colour,
    blend,
    glyph_for,
    remember_row_seen,
    request_refresh,
    rows_to_show,
    snapshot_changed_at,
    sorted_rows,
)
from .theme import PALETTE

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

# The date is drawn on a scale of its own, so it needs finding among the columns.
DATE_COLUMN = "when"

# What the table widget understands as a right click. It adds the other button macOS uses for one itself, so this
# is the same everywhere.
RIGHT_CLICK = "<3>"

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


def cells_of(entry: Row, glyphs: bool) -> list[str]:
    """Return one row's text, in column order.

    :param entry: the row to lay out
    :param glyphs: whether the window can draw the marks that head a row
    """
    label = f"{glyph_for(entry)}  {entry.label}" if glyphs else entry.label
    return [label, entry.repo, entry.number, entry.title, entry.who, entry.when]


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
        self.entries = entries
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
        self.root.configure(background=PALETTE.border)
        self.body = tk.Frame(self.root, background=PALETTE.background)
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

    def heading_text(self) -> str:
        """Return the line at the top of the window, which counts the rows not yet marked seen."""
        waiting = sum(1 for entry in self.entries if not entry.seen)
        return f"{APP_NAME} - {waiting} notification{'' if waiting == 1 else 's'}" if waiting else f"{APP_NAME} - nothing to do"

    def build(self) -> None:
        """Lay out the heading strip, the table and the closing hint."""
        self.heading_strip(self.heading_text())
        if self.entries:
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
        close = tk.Label(strip, text="X", background=PALETTE.background, foreground=PALETTE.muted, font=self.bold, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: self.close())
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
        self.sheet.set_column_widths([self.characters(width) for _key, _heading, width, _stretches in COLUMNS])
        self.paint()
        self.sheet.bind("<Button-1>", self.on_click, add="+")
        self.sheet.bind(RIGHT_CLICK, self.on_right_click)

    def paint(self) -> None:
        """Colour every cell.

        A row is drawn in the colour of what it is, at full strength while it wants attention and dimmed once seen.
        That is the only thing that dims it: how old something is has a scale of its own in the date column, which
        runs from just-happened to long-forgotten and reads as a gradient down the window. Dimming for age as well
        left two rows of the same sort looking different for a reason nobody could name.
        """
        self.sheet.dehighlight_cells(all_=True)
        date_column = next(index for index, (key, *_rest) in enumerate(COLUMNS) if key == DATE_COLUMN)
        for row, entry in enumerate(self.entries):
            body = blend(entry.colour, PALETTE.background, SEEN_STRENGTH) if entry.seen else entry.colour
            date = age_colour(entry.at)
            for column in range(len(COLUMNS)):
                self.sheet.highlight_cells(row=row, column=column, fg=date if column == date_column else body)

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
        self.entries[row] = replace(self.entries[row], seen=seen)
        remember_row_seen(self.entries[row], seen)
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
        """Draw the closing hint and the button that asks for a fresh look."""
        strip = tk.Frame(self.body, background=PALETTE.background)
        strip.pack(fill="x", side="bottom")
        self.hint = tk.Label(
            strip,
            text="Click a row to open it, right-click to mark it seen. Click a heading to sort, drag the title to move, an edge to resize.",
            background=PALETTE.background,
            foreground=PALETTE.muted,
            font=self.regular,
            anchor="w",
            padx=12,
            pady=6,
        )
        self.hint.pack(side="left")
        self.refresh_button = tk.Label(
            strip,
            text="Refresh",
            background=PALETTE.surface,
            foreground=PALETTE.heading,
            font=self.bold,
            cursor="hand2",
            padx=10,
            pady=2,
        )
        self.refresh_button.pack(side="right", padx=12, pady=4)
        self.refresh_button.bind("<Button-1>", lambda _event: self.refresh())

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
        """Read the stored data again and redraw the table in the order currently chosen."""
        self.entries = sorted_rows(rows_to_show(load_config()["popup_rows"]), self.sort_column, self.newest_first)
        if hasattr(self, "sheet"):
            self.refill()

    def edge_handles(self) -> None:
        """Put a grab strip along every edge and corner, so the window resizes from wherever the pointer lands."""
        for _name, edges, cursor, place in EDGE_HANDLES:
            handle = tk.Frame(self.root, background=PALETTE.border, cursor=cursor)
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
        """Put the window near the pointer, sized to its contents and kept clear of the desktop's own bars.

        The height follows how many rows there actually are, so a quiet day gets a small window rather than a tall
        one mostly full of nothing.
        """
        self.root.update_idletasks()
        usable_width, usable_height = self.usable_screen()
        tallest = int(usable_height * TALLEST_SHARE_OF_SCREEN)
        if hasattr(self, "sheet"):
            self.sheet.configure(height=min(self.table_height(), tallest))
            self.root.update_idletasks()
        width = min(max(MINIMUM_WIDTH, self.preferred_width()), usable_width - 2 * EDGE_MARGIN)
        height = min(max(MINIMUM_HEIGHT, self.root.winfo_reqheight()), tallest)
        left = min(max(EDGE_MARGIN, self.root.winfo_pointerx() - width + POINTER_OFFSET), usable_width - width - EDGE_MARGIN)
        # The window sits above the pointer, and never below what the desktop says is usable, so a click low on the
        # screen does not put it behind the taskbar.
        top = min(max(EDGE_MARGIN, self.root.winfo_pointery() - height - POINTER_GAP), usable_height - height - EDGE_MARGIN)
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
        logger.debug("showing {} rows in the {} theme", len(self.entries), "dark" if PALETTE.dark else "light")
        self.root.mainloop()


def show_popup() -> None:
    """Show what wants attention in a frameless window, as many rows as the settings ask for."""
    Popup(rows_to_show(load_config()["popup_rows"])).show()
