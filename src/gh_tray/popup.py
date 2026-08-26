"""A small frameless window listing the most recent changes, opened by a click on the tray icon.

It runs as its own process, like the settings window, so its user interface loop never shares a thread with the tray
icon's. That also keeps it working on macOS, where a window may only be built on a process's main thread.

Having no frame means the window has none of the things a frame normally provides, so each is supplied here: it is
moved by dragging its heading strip, resized by dragging its bottom strip or right edge, scrolled with the wheel,
and closed by Escape, the close mark, or clicking anything else on screen.
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import font as tkfont

from . import APP_NAME
from .config import load_config
from .environment import make_dpi_aware
from .events import age_in_words, is_urgent, label_for, last_seen, moment, recent_events

EDGE_MARGIN = 12
# The window is opened by a click, so it appears by the pointer. Nudging it up and left keeps it clear of the
# pointer itself and, when the click was on a tray icon, clear of the taskbar.
POINTER_OFFSET = 16
MINIMUM_WIDTH = 420
MINIMUM_HEIGHT = 120

FONT_SIZE = 11
POINTS_PER_INCH = 72.0
EDGE_HANDLE_WIDTH = 5

BACKGROUND = "#0d1117"
BORDER = "#30363d"
HEADING = "#e6edf3"
TEXT = "#f0f6fc"
MUTED = "#9198a1"
LINK = "#79c0ff"
URGENT = "#ff7b72"
ROUTINE = "#e3b341"
HOVER = "#21262d"

# Each column: its heading and how many characters wide it is. Widths are in characters of the window's font, and
# every cell in a column is given the same one, so the headings line up with the rows whatever the text in them.
# The title column has no width because it takes whatever space is left, which is what resizing the window changes.
COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("Change", 17),
    ("Repository", 26),
    ("PR", 6),
    ("Title", 0),
    ("Who", 15),
    ("When", 9),
)
STRETCHING_COLUMN = "Title"

TITLE_LIMIT = 70


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
    """Return the colour of a row's marker.

    :param event: the change the row describes
    :param unread: whether it arrived since the user last looked
    """
    if not unread:
        return MUTED
    return URGENT if is_urgent(event["kind"]) else ROUTINE


def rows_to_show(count: int) -> list[tuple[dict, bool]]:
    """Return the changes to list, newest first, each paired with whether it is still unread.

    :param count: how many to show
    """
    marker = last_seen()
    since = moment(marker) if marker else None
    return [(event, since is None or moment(event["at"]) > since) for event in recent_events(count)]


class Popup:
    """The frameless window itself."""

    def __init__(self, entries: list[tuple[dict, bool]]) -> None:
        """:param entries: the changes to list, each paired with whether it is unread."""
        make_dpi_aware()
        self.entries = entries
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

    def label(self, parent: tk.Widget, text: str, colour: str, width: int, bold: bool = False) -> tk.Label:
        """Make one cell of the table.

        :param parent: the row to put it in
        :param text: what it says
        :param colour: the colour of that text
        :param width: how many characters wide, or zero to take whatever space is left
        :param bold: whether to draw it heavier
        """
        cell = tk.Label(
            parent,
            text=text,
            background=BACKGROUND,
            foreground=colour,
            font=self.bold if bold else self.regular,
            anchor="w",
            padx=4,
        )
        if width:
            cell.configure(width=width)
        cell.pack(side="left", expand=not width, fill="x" if not width else None)
        return cell

    def build(self) -> None:
        """Lay out the heading strip, the column headings and one scrollable row per change."""
        unread = sum(1 for _event, is_new in self.entries if is_new)
        self.heading_strip(f"{APP_NAME} - {unread} unread" if unread else f"{APP_NAME} - nothing unread")
        if not self.entries:
            tk.Label(
                self.body,
                text="No changes recorded yet.",
                background=BACKGROUND,
                foreground=MUTED,
                font=self.regular,
                anchor="w",
                padx=12,
                pady=14,
            ).pack(fill="x")
            self.footer()
            return
        self.column_headings()
        self.scrolling_rows()
        self.footer()

    def scrolling_rows(self) -> None:
        """Lay the rows out in an area that scrolls, so making the window taller shows more of them.

        The headings stay outside it, since a heading that scrolled away with the rows would stop being a heading.
        """
        self.canvas = tk.Canvas(self.body, background=BACKGROUND, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True, padx=6)
        self.rows = tk.Frame(self.canvas, background=BACKGROUND)
        self.canvas.create_window((0, 0), window=self.rows, anchor="nw", tags="rows")
        for event, is_new in self.entries:
            self.row(event, is_new)
        self.rows.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure("rows", width=event.width))
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(sequence, self.scroll)
        self.rows.update_idletasks()
        self.canvas.configure(height=min(self.rows.winfo_reqheight(), self.root.winfo_screenheight() // 2))

    def scroll(self, event: tk.Event) -> None:
        """Scroll the rows by a wheel notch, whichever way this platform reports one.

        X11 reports a wheel as button four or five; Windows and macOS report a signed amount instead.
        """
        upwards = event.num == 4 if getattr(event, "num", 0) in (4, 5) else event.delta > 0
        self.canvas.yview_scroll(-1 if upwards else 1, "units")

    def heading_strip(self, text: str) -> None:
        """Draw the title strip, which names the window and is what it is dragged by."""
        strip = tk.Frame(self.body, background=BACKGROUND, cursor="fleur")
        strip.pack(fill="x", padx=12, pady=(10, 6))
        name = tk.Label(strip, text=text, background=BACKGROUND, foreground=HEADING, font=self.bold)
        name.pack(side="left")
        close = tk.Label(strip, text="X", background=BACKGROUND, foreground=MUTED, font=self.bold, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: self.close())
        for widget in (strip, name):
            widget.bind("<Button-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.move_window)

    def column_headings(self) -> None:
        """Draw the column headings, so each row's cells can be read without guessing what they are."""
        line = tk.Frame(self.body, background=BACKGROUND)
        line.pack(fill="x", padx=6, pady=(2, 0))
        for heading, width in COLUMNS:
            self.label(line, heading, HEADING, width, bold=True)
        tk.Frame(self.body, background=BORDER, height=1).pack(fill="x", padx=10, pady=(3, 3))

    def row(self, event: dict, unread: bool) -> None:
        """Draw one change as a clickable row.

        :param event: the change to describe
        :param unread: whether it arrived since the user last looked
        """
        url = event.get("url", "")
        repo, number = repo_and_number(event)
        line = tk.Frame(self.rows, background=BACKGROUND, cursor="hand2" if url else "arrow")
        line.pack(fill="x")
        cells = (
            ("*", dot_colour(event, unread), True),
            (label_for(event["kind"]), TEXT if unread else MUTED, unread),
            (repo, LINK if url else TEXT, False),
            (number, LINK if url else TEXT, False),
            (str(event.get("title") or event.get("detail", ""))[:TITLE_LIMIT], TEXT if unread else MUTED, False),
            (event.get("actor", ""), MUTED, False),
            (age_in_words(event["at"]), MUTED, False),
        )
        for (text, colour, bold), (_heading, width) in zip(cells, COLUMNS, strict=True):
            self.label(line, text, colour, width, bold=bold)
        for widget in (line, *line.winfo_children()):
            widget.bind("<Button-1>", lambda _event, address=url: self.open(address))
            widget.bind("<Enter>", lambda _event, target=line: self.shade(target, HOVER))
            widget.bind("<Leave>", lambda _event, target=line: self.shade(target, BACKGROUND))

    def footer(self) -> None:
        """Draw the closing hint, and make the whole bottom strip a place the window can be resized from.

        The strip is the handle rather than only a corner mark, because a one-character target is hard to hit.
        """
        strip = tk.Frame(self.body, background=BACKGROUND, cursor="sizing")
        strip.pack(fill="x", side="bottom")
        hint = tk.Label(
            strip,
            text="Click a row to open it. Wheel scrolls. Drag the title to move, this strip to resize. Escape closes.",
            background=BACKGROUND,
            foreground=MUTED,
            font=self.regular,
            anchor="w",
            padx=12,
            pady=6,
            cursor="sizing",
        )
        hint.pack(side="left")
        grip = tk.Label(strip, text="//", background=BACKGROUND, foreground=MUTED, font=self.bold, cursor="sizing", padx=10)
        grip.pack(side="right")
        for widget in (strip, hint, grip):
            widget.bind("<Button-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.resize_window)

    def edge_handle(self) -> None:
        """Add a strip down the right-hand edge that the window can also be resized from."""
        edge = tk.Frame(self.root, background=BORDER, width=EDGE_HANDLE_WIDTH, cursor="sb_h_double_arrow")
        edge.place(relx=1.0, rely=0.0, anchor="ne", relheight=1.0)
        edge.bind("<Button-1>", self.start_drag)
        edge.bind("<B1-Motion>", self.resize_window)

    def shade(self, line: tk.Frame, colour: str) -> None:
        """Shade a row so it is clear which one a click would open."""
        line.configure(background=colour)
        for child in line.winfo_children():
            child.configure(background=colour)

    def start_drag(self, event: tk.Event) -> None:
        """Remember where a drag began, and how big and where the window was when it did."""
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())
        self.window_origin = (self.root.winfo_x(), self.root.winfo_y())

    def move_window(self, event: tk.Event) -> None:
        """Move the window by however far the pointer has travelled since the drag began."""
        start_x, start_y, _width, _height = self.drag_origin
        left, top = self.window_origin
        self.root.geometry(f"+{left + event.x_root - start_x}+{top + event.y_root - start_y}")

    def resize_window(self, event: tk.Event) -> None:
        """Resize the window by however far the pointer has travelled since the drag began."""
        start_x, start_y, width, height = self.drag_origin
        self.root.geometry(f"{max(MINIMUM_WIDTH, width + event.x_root - start_x)}x{max(MINIMUM_HEIGHT, height + event.y_root - start_y)}")

    def open(self, url: str) -> None:
        """Open a change on GitHub and close the window.

        :param url: the page to open; a row without one only closes the window
        """
        if url:
            webbrowser.open(url)
        self.close()

    def close(self) -> None:
        """Take the window down."""
        self.root.destroy()

    def place(self) -> None:
        """Put the window near the pointer, sized to its contents and kept fully on screen.

        The size comes from what was laid out rather than from a fixed number, so a column can be widened without
        the last one being quietly cut off.
        """
        self.root.update_idletasks()
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(self.root.winfo_reqwidth(), screen_width - 2 * EDGE_MARGIN)
        height = min(self.root.winfo_reqheight(), screen_height - 2 * EDGE_MARGIN)
        left = min(max(EDGE_MARGIN, self.root.winfo_pointerx() - width + POINTER_OFFSET), screen_width - width - EDGE_MARGIN)
        top = min(max(EDGE_MARGIN, self.root.winfo_pointery() - height - POINTER_OFFSET), screen_height - height - EDGE_MARGIN)
        self.root.geometry(f"{width}x{height}+{int(left)}+{int(top)}")

    def show(self) -> None:
        """Display the window and wait until it is dismissed."""
        self.edge_handle()
        self.place()
        self.root.deiconify()
        self.root.focus_force()
        self.root.bind("<Escape>", lambda _event: self.close())
        # Binding this straight away can close the window before it has finished taking focus.
        self.root.after(300, lambda: self.root.bind("<FocusOut>", lambda _event: self.close()))
        self.root.mainloop()


def show_popup() -> None:
    """Show the most recent changes in a frameless window, as many as the settings ask for."""
    Popup(rows_to_show(load_config()["popup_rows"])).show()
