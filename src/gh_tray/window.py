"""The changes window: a small tool window listing what wants the user's attention, shown by a click on the tray icon.

Each cell is coloured on its own: a row says what it is in one colour and how stale it is in another, and neither
has to give way to the other. Clicking a row opens it on GitHub. Right-clicking marks it seen, and right-clicking
again marks it unseen.

The window is built once, when the tray starts, and hidden rather than closed, so showing it costs nothing.

It has no frame, so the little a frame provides is supplied here: a strip at the top to drag it by, edges to resize
it from, a close mark, and a button carrying the tray's menu, which some desktops offer no other way to reach. The
dragging and resizing themselves are handed to the desktop, which does them as it does for any window. Escape, the
close mark, a click on the tray icon, or a click anywhere else on screen put it away.
"""

from __future__ import annotations

import time
import webbrowser
from dataclasses import replace

from loguru import logger
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME
from .config import APP_ICON_PATH, load_config
from .popup import (
    COLUMNS,
    DEFAULT_SORT,
    FILTER_CHOICES,
    QUIET,
    SEEN_STRENGTH,
    STATUS_COLOURS,
    Row,
    age_colour,
    closed_matches,
    glyph_for,
    matches_search,
    name_colour,
    org_and_name,
    remember_row_seen,
    role_matches,
    row_background,
    rows_to_show,
    sorted_rows,
)
from .status import write_app_icon
from .theme import Palette, blend, chosen_style, ink, palette
from .toolkit import layout_store

EDGE_MARGIN = 12
# The window is opened by a click, so it appears by the pointer. Nudging it up and left keeps it clear of the
# pointer itself and, when the click was on a tray icon, clear of the taskbar.
POINTER_OFFSET = 16
# How far above the pointer the window sits, so a click near the bottom of the screen still leaves it clear.
POINTER_GAP = 24
MINIMUM_WIDTH = 480
MINIMUM_HEIGHT = 140
# Room for the window's edges, the scrollbar and the table's own padding, so no column starts out cut off.
WIDTH_ALLOWANCE = 70
# A window wide enough for the longest repository name anyone owns, at the largest text, would be the whole screen,
# so it is capped here. Columns that still do not fit give up width alike rather than scrolling sideways.
WIDEST_SHARE_OF_SCREEN = 0.9
# The column that takes whatever width is left over, and gives it up first when there is too little: the one with
# the most to say. The shortest any column is squeezed to, in characters.
FILLING_COLUMN = next(name for name, _heading, _width, fills in COLUMNS if fills)
SHORTEST_COLUMN = 4
# The most of the screen the window may take up. Beyond that the rows scroll.
TALLEST_SHARE_OF_SCREEN = 0.55
# What a row has around its text, so rows read as rows rather than as lines.
ROW_PADDING = 10
# How wide the border is that the window can be resized from. It is also the margin around the contents, so that
# a press anywhere in the margin takes an edge.
GRIP = 8
# How long the window takes to finish coming up and settle on the focus. Until then a loss of focus is part of it
# arriving rather than the user clicking elsewhere, and dismissing on that would mean it never appeared at all.
FOCUS_SETTLE_SECONDS = 0.3
# How soon after the window loses the focus a click on the tray icon counts as the click that took it. That click
# means put the window away, and answering it by showing the window again would leave it having done nothing.
TOGGLE_WITHIN_SECONDS = 0.5

# The date and the status are each drawn on a scale of their own, and the four columns that hold a name are drawn in
# the colour dealt to that name, so they need finding among the columns.
DATE_COLUMN = "when"
STATUS_COLUMN = "status"
NAMED_COLUMNS = ("org", "repo", "author", "who")

# Where the remembered widths are kept in the layout store: the window's, and each column's by its name. Named
# rather than positional, so a column added or moved later cannot inherit the wrong width. Kept in characters of the
# window's font rather than in pixels, so they come out the same on a display drawing at another size, and follow the
# text when it is zoomed.
WIDTH_KEY = "window/characters"
COLUMN_KEY = "columns/{}/characters"

HINT = (
    "Click a row to open it, right-click to mark it seen. Click a heading to sort. "
    "Drag the title to move, an edge to resize. Ctrl and the wheel size the text."
)


def column_of(key: str) -> int:
    """Return where a named column sits in the table.

    :param key: the name a column is known by
    """
    return next(index for index, (name, *_rest) in enumerate(COLUMNS) if name == key)


def cells_of(entry: Row) -> list[str]:
    """Return one row's text, in column order.

    :param entry: the row to lay out
    """
    owner, name = org_and_name(entry.repo)
    return [
        f"{glyph_for(entry)}  {entry.label}",
        owner,
        name,
        entry.number,
        entry.status,
        entry.title,
        entry.author,
        entry.who,
        entry.when,
    ]


class ChangesWindow(QWidget):
    """The window itself.

    It says two things to the tray: that the user pressed Refresh, and that they pressed Open dashboard. The tray is
    the only thing allowed to poll, and the dashboard is its to open.
    """

    refresh_asked = Signal()
    dashboard_asked = Signal()

    def __init__(self, entries: list[Row], layout: QSettings | None = None) -> None:
        """Build the window, hidden.

        :param entries: the lines to list, in the order they should appear
        :param layout: where remembered sizes are kept, defaulting to the application's own layout file
        """
        super().__init__(
            None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        # So the pointer changes shape over an edge before anything is pressed.
        self.setMouseTracking(True)
        # Everything on offer, and the part of it the chosen filters let through, which is what the table shows.
        # Marks are written to both, so switching filters does not forget them.
        self.all_entries = list(entries)
        self.entries: list[Row] = []
        self.role_filter = "all"
        # Rows about closed pull requests start hidden: they are done, and the window is a list of what is not.
        self.show_closed = False
        self.sort_column = DEFAULT_SORT
        self.newest_first = True
        # What the search box holds, which every row shown has to contain somewhere.
        self.search_text = ""
        self.inks: Palette = palette(chosen_style())
        self.layout_store = layout if layout is not None else layout_store()
        # The width this code last gave the window. A resize to anything else is the user's doing, and worth keeping.
        self.placed_width = 0
        # Whether a fresh look has been asked for and not yet answered, which is when the hint says so.
        self.awaiting_poll = False
        # Whether the columns are being sized by this code, whose sizes are not the user's and are not remembered.
        self.fitting = False
        # When the window last came up, and when a click elsewhere last put it away. No sentinel number for the
        # second: the reference point of a monotonic clock is undefined, so any number chosen to mean "never" could
        # legitimately occur.
        self.shown_at = 0.0
        self.dismissed_at: float | None = None
        # Whether the desktop is moving or resizing the window on the application's behalf. Some desktops, GNOME
        # among them, take the activation for the whole of such a drag, and losing it then is not a click elsewhere.
        self.desktop_dragging = False
        try:
            self.setWindowIcon(QIcon(str(write_app_icon(APP_ICON_PATH))))
        except OSError as error:
            logger.debug("could not put the application's mark on the window: {}", error)
        self.build()
        self.apply_filter()
        self.refill()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.on_scheme_changed)

    def build(self) -> None:
        """Lay out the title strip, the table, the strip of controls under it, and the hint at the bottom."""
        column = QVBoxLayout(self)
        column.setContentsMargins(GRIP, GRIP, GRIP, GRIP)
        column.addWidget(self.title_strip())
        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels([heading for _key, heading, _width, _stretches in COLUMNS])
        self.table.verticalHeader().hide()
        # Nothing is ever selected: a click opens the row and puts the window away, so a selection could only be the
        # last thing clicked, and the desktop's style draws one as a frame at the edge of every cell in the row.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        # Sorted here rather than by the widget, so dates sort as moments and numbers as numbers.
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(column_of(DEFAULT_SORT), Qt.SortOrder.DescendingOrder)
        header.setSectionsClickable(True)
        header.setStretchLastSection(False)
        header.sectionClicked.connect(self.on_heading_clicked)
        header.sectionResized.connect(self.on_column_resized)
        self.table.cellClicked.connect(self.on_cell_clicked)
        # Right clicks are seen before the table does anything with them, since the table has no say in marking.
        self.table.viewport().installEventFilter(self)
        self.size_rows()
        self.size_columns()
        column.addWidget(self.table, 1)
        column.addLayout(self.controls())
        # A shortcut rather than a key handler, so it works whichever part of the window has the focus.
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.activated.connect(self.focus_search)
        self.hint = QLabel(HINT, self)
        column.addWidget(self.hint)

    def title_strip(self) -> QWidget:
        """Lay out the strip at the top: it names the window, is what it is dragged by, and carries the close mark."""
        self.strip = QWidget(self)
        self.strip.setCursor(Qt.CursorShape.SizeAllCursor)
        row = QHBoxLayout(self.strip)
        row.setContentsMargins(4, 0, 0, 2)
        self.name = QLabel(self.heading_text(), self.strip)
        bold = self.name.font()
        bold.setBold(True)
        self.name.setFont(bold)
        row.addWidget(self.name)
        row.addStretch(1)
        # The tray's menu, hung off a button so it opens under it: anchored to this window, it lands where it should
        # even on a desktop that will not place a window by a screen coordinate. Hidden until the tray hands it over.
        self.menu_button = QToolButton(self.strip)
        self.menu_button.setText("Menu")
        self.menu_button.setAutoRaise(True)
        self.menu_button.setCursor(Qt.CursorShape.ArrowCursor)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.hide()
        row.addWidget(self.menu_button)
        self.close_mark = QToolButton(self.strip)
        self.close_mark.setAutoRaise(True)
        self.close_mark.setCursor(Qt.CursorShape.ArrowCursor)
        self.close_mark.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton))
        self.close_mark.clicked.connect(self.hide)
        row.addWidget(self.close_mark)
        return self.strip

    def attach_menu(self, menu: QMenu) -> None:
        """Hang the tray's menu off the button in the title strip, and show the button.

        :param menu: the tray's menu, which the tray keeps rebuilding in place
        """
        self.menu_button.setMenu(menu)
        self.menu_button.show()

    def controls(self) -> QHBoxLayout:
        """Lay out the quick filters, the closed toggle, the search box, and the dashboard and refresh buttons."""
        strip = QHBoxLayout()
        self.filters = QButtonGroup(self)
        self.chips: dict[str, QPushButton] = {}
        for name, label in FILTER_CHOICES:
            chip = QPushButton(label, self)
            chip.setCheckable(True)
            chip.setChecked(name == self.role_filter)
            chip.clicked.connect(lambda _checked=False, wanted=name: self.choose_filter(wanted))
            self.filters.addButton(chip)
            strip.addWidget(chip)
            self.chips[name] = chip
        strip.addSpacing(12)
        # Set apart from the quick filters, since it works alongside them rather than instead of them.
        self.closed_chip = QPushButton("Show closed", self)
        self.closed_chip.setCheckable(True)
        self.closed_chip.toggled.connect(self.set_show_closed)
        strip.addWidget(self.closed_chip)
        # The search takes whatever room the buttons leave, so the strip stays one line.
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(self.characters(14))
        self.search.textChanged.connect(self.set_search)
        strip.addSpacing(12)
        strip.addWidget(self.search, 1)
        self.dashboard_button = QPushButton("Open dashboard", self)
        self.dashboard_button.clicked.connect(self.open_dashboard)
        strip.addWidget(self.dashboard_button)
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.clicked.connect(self.refresh)
        strip.addWidget(self.refresh_button)
        return strip

    def advance(self) -> int:
        """Return how wide one character is in this window's font, which is the unit every width is kept in."""
        return self.fontMetrics().horizontalAdvance("0")

    def characters(self, count: float) -> int:
        """Return how wide a number of characters is in this window's font, in pixels.

        :param count: how many characters
        """
        return round(self.advance() * count)

    def remembered(self, key: str) -> float:
        """Return a remembered width in characters, or zero when nothing is remembered under that name.

        :param key: where in the layout store to look
        """
        stored = self.layout_store.value(key, 0.0, float)
        return stored if isinstance(stored, float) and stored > 0 else 0.0

    def remember(self, key: str, pixels: int) -> None:
        """Record a width, in characters of the font as it is now.

        :param key: where in the layout store to keep it
        :param pixels: the width as the window has it
        """
        self.layout_store.setValue(key, round(pixels / self.advance(), 2))

    def size_rows(self) -> None:
        """Make every row as tall as a line of the window's text wants, with room around it."""
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + ROW_PADDING)

    def size_columns(self) -> None:
        """Give each column the width the user last dragged it to, or its starting size, then fit them to the table."""
        self.fitting = True
        try:
            for index, (key, _heading, width, _fills) in enumerate(COLUMNS):
                self.table.setColumnWidth(index, self.characters(self.remembered(COLUMN_KEY.format(key)) or width))
        finally:
            self.fitting = False
        self.fit_columns(shrink_all=True)

    def fit_columns(self, shrink_all: bool) -> None:
        """Make the columns fill the table exactly, so nothing is left empty and nothing has to scroll sideways.

        Whatever is left over goes to the filling column, which also gives way first when there is too little. When
        even that at its shortest leaves the rest too wide, every other column gives up the same share, which suits
        the text having been zoomed but not a column the user has just dragged wider on purpose.

        :param shrink_all: whether the other columns may be squeezed as well
        """
        # A table not yet shown has no width worth fitting to; the showing brings a resize that fits it then.
        room = self.table.viewport().width()
        if room <= 0 or not self.isVisible():
            return
        filling = column_of(FILLING_COLUMN)
        least = self.characters(SHORTEST_COLUMN)
        others = [index for index in range(len(COLUMNS)) if index != filling]
        self.fitting = True
        try:
            taken = sum(self.table.columnWidth(index) for index in others)
            if shrink_all and taken + least > room:
                scale = (room - least) / taken
                for index in others:
                    self.table.setColumnWidth(index, max(least, round(self.table.columnWidth(index) * scale)))
                taken = sum(self.table.columnWidth(index) for index in others)
            self.table.setColumnWidth(filling, max(least, room - taken))
        finally:
            self.fitting = False

    def on_column_resized(self, index: int, _was: int, width: int) -> None:
        """Remember a column's width once the user has dragged it, and let the filling column take up the difference.

        Widths set by this code, and any set while the window is hidden, which is when it is put together, are not
        the user's and are left alone. A drag of the filling column itself stands as dragged.
        """
        if self.fitting or index >= len(COLUMNS) or not self.isVisible():
            return
        self.remember(COLUMN_KEY.format(COLUMNS[index][0]), width)
        if index != column_of(FILLING_COLUMN):
            self.fit_columns(shrink_all=False)

    def on_font_changed(self) -> None:
        """Size the rows and columns for the text as it now is, and the window around them."""
        self.size_rows()
        self.size_columns()
        self.refit(resize_width=True)

    def heading_text(self) -> str:
        """Return the window's title, which counts the rows not yet marked seen."""
        waiting = sum(1 for entry in self.entries if not entry.seen)
        return (
            f"{APP_NAME} - {waiting} notification{'' if waiting == 1 else 's'}"
            if waiting
            else f"{APP_NAME} - nothing to do"
        )

    def apply_filter(self) -> None:
        """Reduce everything on offer to what the chosen filters let through, in the chosen order."""
        kept = [
            entry
            for entry in self.all_entries
            if role_matches(entry, self.role_filter)
            and closed_matches(entry, self.show_closed)
            and matches_search(entry, self.search_text)
        ]
        self.entries = sorted_rows(kept, self.sort_column, self.newest_first)

    def refill(self) -> None:
        """Put the rows into the table in their current order, colour them, and count the unread ones in the title."""
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            for column, text in enumerate(cells_of(entry)):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.paint()
        self.setWindowTitle(self.heading_text())
        self.name.setText(self.heading_text())

    def paint(self) -> None:
        """Colour every cell.

        A row is drawn in the ink of what it is, at full strength while it wants attention and dimmed once seen.
        That is the only thing that dims it: how old something is has a scale of its own in the date column, which
        runs from just-happened to long-forgotten and reads as a gradient down the window. Dimming for age as well
        left two rows of the same sort looking different for a reason nobody could name.

        A name has an ink of its own too, dealt to it and kept, so the same person, organisation or repository
        reads as the same colour in every row and rows about the same one group by eye. It dims with the rest of the
        row once seen, unlike the date, whose scale is the point of it.

        Dimming and washes are mixed towards the colour the toolkit actually painted the table, so they sit right in
        whichever theme the desktop is drawing.
        """
        ground = self.table.palette().base().color().name()
        date_column = column_of(DATE_COLUMN)
        for row, entry in enumerate(self.entries):
            owner, _name = org_and_name(entry.repo)
            named = {"org": owner, "repo": entry.repo, "author": entry.author, "who": entry.who}
            inks = {
                date_column: age_colour(entry.at, self.inks),
                column_of(STATUS_COLUMN): ink(self.inks, STATUS_COLOURS.get(entry.status, QUIET)),
                **{column_of(key): ink(self.inks, name_colour(named[key])) for key in NAMED_COLUMNS},
            }
            # A finished pull request's row sits on a wash of its status colour, so it reads as done at a glance.
            wash = row_background(entry, self.inks, ground)
            for column in range(len(COLUMNS)):
                colour = inks.get(column) or ink(self.inks, entry.colour)
                if entry.seen and column != date_column:
                    colour = blend(colour, ground, SEEN_STRENGTH)
                item = self.table.item(row, column)
                if item is None:
                    continue
                item.setForeground(QBrush(QColor(colour)))
                item.setBackground(QBrush(QColor(wash)) if wash else QBrush())

    def on_cell_clicked(self, row: int, _column: int) -> None:
        """Open the row that was left-clicked on GitHub, and put the window away.

        :param row: which row was clicked
        """
        if 0 <= row < len(self.entries) and self.entries[row].url:
            self.open(self.entries[row].url)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Mark a row seen when it is right-clicked, and fit the columns when the table changes size.

        Every other event is the table's own.

        :param watched: whatever the event was sent to
        :param event: the event
        """
        if watched is not self.table.viewport():
            return super().eventFilter(watched, event)
        if (
            isinstance(event, QMouseEvent)
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.RightButton
        ):
            row = self.table.rowAt(int(event.position().y()))
            if 0 <= row < len(self.entries):
                self.set_seen(row, not self.entries[row].seen)
            return True
        if event.type() == QEvent.Type.Resize:
            self.size_columns()
        return super().eventFilter(watched, event)

    def set_seen(self, row: int, seen: bool) -> None:
        """Mark one row seen or unseen, remember it for next time, and redraw.

        :param row: which row to mark
        :param seen: whether the user has now seen it
        """
        if self.entries[row].seen == seen:
            return
        marked = replace(self.entries[row], seen=seen)
        self.entries[row] = marked
        self.all_entries = [
            marked if entry.url == marked.url and entry.at == marked.at else entry for entry in self.all_entries
        ]
        remember_row_seen(marked, seen)
        self.refill()

    def on_heading_clicked(self, index: int) -> None:
        """Sort by the column whose heading was clicked.

        :param index: which heading
        """
        if index < len(COLUMNS):
            self.sort_by(COLUMNS[index][0])

    def sort_by(self, column: str) -> None:
        """Reorder the table by a column, turning the order around when it is already the one being sorted by.

        :param column: the column whose heading was clicked
        """
        # Dates read most usefully newest first, everything else A to Z, so each column starts the way it is wanted.
        self.newest_first = not self.newest_first if column == self.sort_column else column == DEFAULT_SORT
        self.sort_column = column
        self.entries = sorted_rows(self.entries, column, self.newest_first)
        self.refill()
        order = Qt.SortOrder.DescendingOrder if self.newest_first else Qt.SortOrder.AscendingOrder
        self.table.horizontalHeader().setSortIndicator(column_of(column), order)

    def choose_filter(self, wanted: str) -> None:
        """Switch the quick filter and redraw around whatever it leaves.

        :param wanted: the filter's name, from :data:`FILTER_CHOICES`
        """
        self.role_filter = wanted
        self.chips[wanted].setChecked(True)
        self.redraw_filtered()

    def set_show_closed(self, wanted: bool) -> None:
        """Show or hide the rows about closed pull requests, and redraw around whatever that leaves.

        :param wanted: whether finished pull requests are wanted in the list
        """
        self.show_closed = wanted
        self.redraw_filtered()

    def set_search(self, text: str) -> None:
        """Keep only the rows holding some text, as it is typed.

        :param text: what the search box now holds
        """
        self.search_text = text
        self.redraw_filtered()

    def focus_search(self) -> None:
        """Put the keyboard in the search box, with whatever it holds selected so typing replaces it."""
        self.search.setFocus()
        self.search.selectAll()

    def redraw_filtered(self) -> None:
        """Redraw around whatever the filters now leave, re-fitting the height from the top edge."""
        self.apply_filter()
        self.refill()
        self.refit()

    def refresh(self) -> None:
        """Ask the tray for a fresh look at GitHub, and say so until it arrives."""
        self.awaiting_poll = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Refreshing")
        self.hint.setText("Asked for a fresh look. This will update when it arrives.")
        self.refresh_asked.emit()

    def on_polled(self, succeeded: bool) -> None:
        """Re-read the stored data now that the tray has finished a poll, so a window that is up stays current.

        :param succeeded: whether the poll reached GitHub
        """
        self.reload()
        if not self.awaiting_poll:
            return
        self.awaiting_poll = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh")
        self.hint.setText(
            "Up to date." if succeeded else "GitHub could not be reached, so this is what was last found."
        )

    def reload(self) -> None:
        """Read the stored data again and redraw the table in the order and filter currently chosen."""
        self.all_entries = rows_to_show(load_config()["popup_rows"])
        self.apply_filter()
        self.refill()

    def usable_screen(self, near: QPoint) -> QRect:
        """Return how much of the screen holding a point a window may use, with any taskbar, dock or panel left out.

        :param near: a point on the screen in question
        :raises RuntimeError: when there is no screen at all
        """
        screen = QGuiApplication.screenAt(near) or QGuiApplication.primaryScreen()
        if screen is None:
            raise RuntimeError("no screen to show a window on")
        return screen.availableGeometry()

    def preferred_width(self, usable: QRect) -> int:
        """Return the width every column needs at its stated size, capped so the window stays a popup.

        :param usable: how much of the screen a window may use
        """
        wanted = sum(self.characters(width) for _key, _heading, width, _stretches in COLUMNS) + WIDTH_ALLOWANCE
        return min(wanted, int(usable.width() * WIDEST_SHARE_OF_SCREEN))

    def wanted_width(self, usable: QRect) -> int:
        """Return how wide the window should come up.

        The width the user dragged it to, where they have, kept on the screen since it may have been dragged out on
        a larger display than this one.

        :param usable: how much of the screen a window may use
        """
        dragged = self.remembered(WIDTH_KEY)
        wanted = self.characters(dragged) if dragged else self.preferred_width(usable)
        # The controls along the bottom set a floor of their own, which the toolkit would enforce anyway, and a width
        # it enforced would otherwise read as the user's doing.
        least = max(MINIMUM_WIDTH, self.minimumSizeHint().width())
        return max(min(wanted, usable.width() - 2 * EDGE_MARGIN), least)

    def table_height(self) -> int:
        """Return how tall the table needs to be to show every row it has, without leaving empty space below."""
        headings = self.table.horizontalHeader().sizeHint().height()
        rows = sum(self.table.rowHeight(row) for row in range(self.table.rowCount()))
        return headings + rows + 2 * self.table.frameWidth()

    def wanted_height(self, usable: QRect) -> int:
        """Return how tall the window should be: snug around a few rows, and no more than its ceiling over many.

        :param usable: how much of the screen a window may use
        """
        around = self.sizeHint().height() - self.table.sizeHint().height()
        tallest = int(usable.height() * TALLEST_SHARE_OF_SCREEN)
        # The controls set a floor of their own, as they do for the width, and it differs from desktop to desktop.
        least = max(MINIMUM_HEIGHT, self.minimumSizeHint().height())
        return max(min(around + self.table_height(), tallest), least)

    def settle_layout(self) -> None:
        """Have the layout work out its sizes now, rather than on the next pass through the event loop.

        The smallest the window may be follows the text, and the toolkit only takes that up when the layout next
        runs. A window placed before then is held to the old floor, and its size would then read as the user's.
        """
        layout = self.layout()
        if layout is not None:
            layout.activate()

    def place(self, geometry: QRect) -> None:
        """Size and position the window, remembering the width as this code's own rather than the user's.

        :param geometry: where the window's contents should sit
        """
        self.placed_width = geometry.width()
        self.setGeometry(geometry)

    def show_by(self, spot: QPoint) -> None:
        """Bring the window up beside a click, sized to its rows, with whatever is currently waiting.

        The height follows how many rows there actually are, so a quiet day gets a small window rather than a tall
        one mostly full of nothing. The window sits above and to the left of the click, and never below what the
        desktop says is usable, so a click low on the screen does not put it behind the taskbar.

        :param spot: where on screen the click was
        """
        self.reload()
        self.settle_layout()
        usable = self.usable_screen(spot)
        width, height = self.wanted_width(usable), self.wanted_height(usable)
        # Held to the screen's left and top edges first: a window wider than the screen keeps its start on it.
        left = max(
            usable.left() + EDGE_MARGIN, min(spot.x() - width + POINTER_OFFSET, usable.right() - width - EDGE_MARGIN)
        )
        top = max(
            usable.top() + EDGE_MARGIN, min(spot.y() - height - POINTER_GAP, usable.bottom() - height - EDGE_MARGIN)
        )
        self.place(QRect(left, top, width, height))
        self.shown_at = time.monotonic()
        self.dismissed_at = None
        self.desktop_dragging = False
        self.show()
        self.raise_()
        self.activateWindow()
        logger.debug(
            "showing {} rows in the {} theme at {}",
            len(self.entries),
            "dark" if self.inks.dark else "light",
            self.geometry(),
        )

    def refit(self, resize_width: bool = False) -> None:
        """Re-fit the window's height to the rows now shown, growing and shrinking from its top edge.

        The bottom edge stays where it is: the window opens above the click, usually just clear of the taskbar, so
        growing downward would take the new rows straight off the bottom of the screen. Growing upward keeps every
        row on it, and the top only gives way when the screen has no more room above.

        :param resize_width: whether to re-fit the width as well, which the text being zoomed calls for
        """
        if not self.isVisible():
            return
        self.settle_layout()
        now = self.geometry()
        usable = self.usable_screen(now.center())
        width = self.wanted_width(usable) if resize_width else now.width()
        height = self.wanted_height(usable)
        left = max(usable.left() + EDGE_MARGIN, min(now.x(), usable.right() - width - EDGE_MARGIN))
        top = max(
            usable.top() + EDGE_MARGIN, min(now.y() + now.height() - height, usable.bottom() - height - EDGE_MARGIN)
        )
        self.place(QRect(left, top, width, height))

    def toggle(self, spot: QPoint) -> None:
        """Show the window by a click, or put it away if it is already up.

        Clicking the tray icon while the window is up takes the focus from it, which puts it away before the click
        is heard here. Answering that click by showing the window again would leave it having done nothing at all,
        so a click this soon after a dismissal is taken as the one that did the dismissing.

        :param spot: where on screen the click was
        """
        if self.isVisible():
            self.hide()
            return
        if self.dismissed_at is not None and time.monotonic() - self.dismissed_at < TOGGLE_WITHIN_SECONDS:
            logger.debug("the click that asked for the window is the one that just put it away")
            self.dismissed_at = None
            return
        self.show_by(spot)

    def open(self, url: str) -> None:
        """Open a change on GitHub and put the window away.

        :param url: the page to open
        """
        webbrowser.open(url)
        self.hide()

    def open_dashboard(self) -> None:
        """Ask the tray to open the dashboard, and put the window away, since the dashboard is about to cover it."""
        self.hide()
        self.dashboard_asked.emit()

    def on_scheme_changed(self, *_scheme: object) -> None:
        """Take the inks of whichever theme the desktop or the settings now ask for, and colour the rows again."""
        self.inks = palette(chosen_style())
        self.paint()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Remember the width the user dragged the window to. Widths this code set are not worth keeping.

        :param event: the resize
        """
        super().resizeEvent(event)
        width = event.size().width()
        # A width equal to the smallest the window may be was the toolkit's doing, holding the window to its floor as
        # the text grew, and not the user's.
        forced = width == self.minimumSize().width()
        if self.isVisible() and width != self.placed_width and not forced:
            self.placed_width = width
            self.remember(WIDTH_KEY, width)

    def edges_at(self, spot: QPoint) -> Qt.Edge:
        """Return which edges of the window a point is within the grip of, which is none for most of it.

        :param spot: a point in the window's own coordinates
        """
        edges = Qt.Edge(0)
        if spot.x() < GRIP:
            edges |= Qt.Edge.LeftEdge
        if spot.x() >= self.width() - GRIP:
            edges |= Qt.Edge.RightEdge
        if spot.y() < GRIP:
            edges |= Qt.Edge.TopEdge
        if spot.y() >= self.height() - GRIP:
            edges |= Qt.Edge.BottomEdge
        return edges

    def start_system_move(self) -> None:
        """Hand the desktop a drag of the whole window, which it carries on until the button is released."""
        handle = self.windowHandle()
        if handle is not None and handle.startSystemMove():
            self.desktop_dragging = True
        else:
            logger.debug("this desktop does not move a window on the application's behalf")

    def start_system_resize(self, edges: Qt.Edge) -> None:
        """Hand the desktop a resize of the window by some of its edges.

        :param edges: which edges are being dragged
        """
        handle = self.windowHandle()
        if handle is not None and handle.startSystemResize(edges):
            self.desktop_dragging = True
        else:
            logger.debug("this desktop does not resize a window on the application's behalf")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Start a move from the title strip or a resize from an edge. Presses anywhere else are the contents' own.

        :param event: the press
        """
        spot = event.position().toPoint()
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        edges = self.edges_at(spot)
        if edges:
            self.start_system_resize(edges)
        elif self.strip.geometry().contains(spot):
            self.start_system_move()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Shape the pointer for the edge it is over, so the window says where it can be resized from.

        :param event: the movement
        """
        edges = self.edges_at(event.position().toPoint())
        shapes = {
            Qt.Edge.LeftEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.RightEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeBDiagCursor,
            Qt.Edge.LeftEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
            Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
        }
        shape = shapes.get(edges)
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """Put the pointer back to its usual shape once it leaves the window.

        :param event: the leaving
        """
        self.unsetCursor()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw a line around the window, since without a frame nothing else says where it ends.

        :param event: what needs painting
        """
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(self.palette().mid().color())
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()

    def event(self, event: QEvent) -> bool:
        """Put the window away when something else takes the focus, once it has settled after coming up.

        A window with no frame has no other way of being clicked away: clicking anything else on screen is what
        takes the focus. A loss of focus in the window's first moments is part of its arriving, not a dismissal,
        and nor is one during a move or resize the desktop is doing on the window's behalf, which on some
        desktops takes the activation for the whole drag.

        :param event: any event the window is sent
        """
        if event.type() == QEvent.Type.WindowActivate:
            # The activation comes back with the first click on the window after such a drag, and losing it after
            # that is the user's doing again.
            self.desktop_dragging = False
        # Visibility is asked first: events arrive while the window is still being built, before it has a show time.
        deactivated = event.type() == QEvent.Type.WindowDeactivate and self.isVisible()
        # A menu of this application's own, popped up from the window, is not somebody clicking elsewhere.
        if deactivated and self.settled() and not self.desktop_dragging and QApplication.activePopupWidget() is None:
            logger.debug("the focus went elsewhere, putting the window away")
            self.hide()
            self.dismissed_at = time.monotonic()
        return super().event(event)

    def settled(self) -> bool:
        """Return whether the window has been up long enough for a loss of focus to be the user's doing."""
        return time.monotonic() - self.shown_at >= FOCUS_SETTLE_SECONDS

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Clear a search on Escape, or put the window away when there is none. Every other key is the table's.

        :param event: the key press
        """
        if event.key() == Qt.Key.Key_Escape:
            if self.search.hasFocus() and self.search.text():
                self.search.clear()
            else:
                self.hide()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Hide rather than close, keeping the window loaded so the next showing is immediate.

        :param event: the close request, which is refused
        """
        event.ignore()
        self.hide()
