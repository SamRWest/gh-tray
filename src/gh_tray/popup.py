"""A small frameless window listing the most recent changes, opened by a click on the tray icon.

It runs as its own process, like the settings window, so its user interface loop never shares a thread with the tray
icon's. That also keeps it working on macOS, where a window may only be built on a process's main thread.

The window has no frame or title bar, so it needs its own ways out: pressing Escape, clicking the close mark, or
clicking anything else on the screen.
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import font as tkfont

from . import APP_NAME
from .config import load_config
from .events import age_in_words, is_urgent, label_for, last_seen, moment, recent_events

WINDOW_WIDTH = 620
EDGE_MARGIN = 12
# The window is opened by a click, so it appears by the pointer. Nudging it up and left keeps it clear of the
# pointer itself and, when the click was on a tray icon, clear of the taskbar.
POINTER_OFFSET = 16

BACKGROUND = "#1c2128"
BORDER = "#444c56"
HEADING = "#768390"
TEXT = "#cdd9e5"
MUTED = "#768390"
LINK = "#6cb6ff"
URGENT = "#e5534b"
ROUTINE = "#c69026"
HOVER = "#2d333b"

TITLE_LIMIT = 46


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
        self.entries = entries
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(background=BORDER)
        self.body = tk.Frame(self.root, background=BACKGROUND)
        self.body.pack(padx=1, pady=1, fill="both", expand=True)
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.build()

    def build(self) -> None:
        """Lay out the header and one row per change."""
        unread = sum(1 for _event, is_new in self.entries if is_new)
        headline = f"{unread} unread" if unread else "nothing unread"
        self.header(f"{APP_NAME} - {headline}")
        if not self.entries:
            tk.Label(
                self.body,
                text="No changes recorded yet.",
                background=BACKGROUND,
                foreground=MUTED,
                anchor="w",
                padx=12,
                pady=14,
            ).pack(fill="x")
            return
        for event, is_new in self.entries:
            self.row(event, is_new)
        tk.Label(
            self.body,
            text="Click a row to open it on GitHub.  Escape closes.",
            background=BACKGROUND,
            foreground=MUTED,
            anchor="w",
            padx=12,
            pady=6,
        ).pack(fill="x")

    def header(self, text: str) -> None:
        """Draw the title strip, which doubles as the only way to see what the window is."""
        strip = tk.Frame(self.body, background=BACKGROUND)
        strip.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(strip, text=text, background=BACKGROUND, foreground=HEADING, font=self.bold).pack(side="left")
        close = tk.Label(strip, text="X", background=BACKGROUND, foreground=MUTED, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: self.close())

    def row(self, event: dict, unread: bool) -> None:
        """Draw one change as a clickable row.

        :param event: the change to describe
        :param unread: whether it arrived since the user last looked
        """
        url = event.get("url", "")
        line = tk.Frame(self.body, background=BACKGROUND, cursor="hand2" if url else "arrow")
        line.pack(fill="x", padx=6)
        cells = [
            ("*", dot_colour(event, unread), 2, self.bold),
            (label_for(event["kind"]), TEXT if unread else MUTED, 18, self.bold if unread else None),
            (event.get("key", ""), LINK if url else TEXT, 34, None),
            (str(event.get("detail", ""))[:TITLE_LIMIT], MUTED, 26, None),
            (age_in_words(event["at"]), MUTED, 9, None),
        ]
        for text, colour, width, face in cells:
            label = tk.Label(line, text=text, background=BACKGROUND, foreground=colour, anchor="w", width=width, padx=3)
            if face is not None:
                label.configure(font=face)
            label.pack(side="left")
        for widget in (line, *line.winfo_children()):
            widget.bind("<Button-1>", lambda _event, address=url: self.open(address))
            widget.bind("<Enter>", lambda _event, target=line: self.highlight(target, HOVER))
            widget.bind("<Leave>", lambda _event, target=line: self.highlight(target, BACKGROUND))

    def highlight(self, line: tk.Frame, colour: str) -> None:
        """Shade a row so it is clear which one a click would open."""
        line.configure(background=colour)
        for child in line.winfo_children():
            child.configure(background=colour)

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
        """Put the window near the pointer, kept fully on screen."""
        self.root.update_idletasks()
        height = self.root.winfo_reqheight()
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        left = min(max(EDGE_MARGIN, self.root.winfo_pointerx() - WINDOW_WIDTH + POINTER_OFFSET), screen_width - WINDOW_WIDTH - EDGE_MARGIN)
        top = min(max(EDGE_MARGIN, self.root.winfo_pointery() - height - POINTER_OFFSET), screen_height - height - EDGE_MARGIN)
        self.root.geometry(f"{WINDOW_WIDTH}x{height}+{int(left)}+{int(top)}")

    def show(self) -> None:
        """Display the window and wait until it is dismissed."""
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
