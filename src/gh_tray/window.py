"""The changes window: a small tool window listing what needs the user's attention.

Double-clicking a row opens it on GitHub; right-clicking toggles it seen or unseen. The tray builds the window
once at startup and hides it rather than closing it, so reopening it is instant. The window is frameless, so this
module draws its own title strip, resize edges, close mark and menu button, with the desktop handling the drags
and resizes.
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
    QHideEvent,
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
# Keeps the window clear of the pointer, and of the taskbar for a tray-icon click.
POINTER_OFFSET = 16
# Extra gap above the pointer, so a click near the screen bottom still clears it.
POINTER_GAP = 24
MINIMUM_WIDTH = 480
MINIMUM_HEIGHT = 140
# Extra width for the window's edges, scrollbar and table padding, so no column starts out cut off.
WIDTH_ALLOWANCE = 70
# Caps width so a long repository name at large text cannot fill the screen; columns shrink instead of scrolling.
WIDEST_SHARE_OF_SCREEN = 0.9
# The column that takes any leftover width, and shrinks first when space is short.
FILLING_COLUMN = next(name for name, _heading, _width, fills in COLUMNS if fills)
SHORTEST_COLUMN = 4
# Beyond this share of the screen height, rows scroll instead of the window growing further.
TALLEST_SHARE_OF_SCREEN = 0.55
# Vertical padding inside each row, so rows read as rows rather than as lines of text.
ROW_PADDING = 10
# Width of the resize border, and the margin around the contents, so a press anywhere in the margin grabs an edge.
GRIP = 8
# A focus loss before this is the window arriving, not a click elsewhere, else it would hide on every showing.
FOCUS_SETTLE_SECONDS = 0.3
# How soon after losing focus a tray-icon click counts as the dismissal, not a fresh request to reopen.
TOGGLE_WITHIN_SECONDS = 0.5

DATE_COLUMN = "when"
STATUS_COLUMN = "status"
NAMED_COLUMNS = ("org", "repo", "author", "who")

# Layout-store keys for remembered widths, keyed by column name so a moved column keeps its own width. Widths are
# stored in characters of the font, not pixels, so they still match after a display-scale or zoom change.
WIDTH_KEY = "window/characters"
COLUMN_KEY = "columns/{}/characters"

# How strongly the clicked row is tinted towards the desktop's highlight colour.
HIGHLIGHT_STRENGTH = 0.3

HINT = (
    "Double-click a row to open it, right-click to mark it seen. Click a heading to sort. "
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
    """The window itself. Emits refresh_asked and dashboard_asked; only the tray polls or opens the dashboard."""

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
        # all_entries holds everything; entries is the filtered subset shown, and marks go to both.
        self.all_entries = list(entries)
        self.entries: list[Row] = []
        self.role_filter = "all"
        # Rows about closed pull requests start hidden: they are done, and the window is a list of what is not.
        self.show_closed = False
        self.sort_column = DEFAULT_SORT
        self.newest_first = True
        self.search_text = ""
        self.inks: Palette = palette(chosen_style())
        self.layout_store = layout if layout is not None else layout_store()
        self.placed_width = 0
        self.awaiting_poll = False
        self.fitting = False
        # dismissed_at is None rather than a sentinel time, since a monotonic clock has no fixed zero point, so no
        # number safely means "never".
        self.shown_at = 0.0
        self.dismissed_at: float | None = None
        # The row last clicked, by URL so it survives sorting and refilling.
        self.highlighted_url: str | None = None
        # Some desktops, GNOME included, hold the window's activation for a whole drag, so losing it then is not
        # a click elsewhere.
        self.desktop_dragging = False
        try:
            self.setWindowIcon(QIcon(str(write_app_icon(APP_ICON_PATH))))
        except OSError as error:
            logger.debug("could not set the window icon: {}", error)
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
        # The toolkit's own selection and focus stay off: the desktop style draws each selected or focused cell with
        # a frame of its own, which shows as a bar in every cell. The clicked row is highlighted by painting it.
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
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        # Catches right clicks before the table acts on them; the table itself has no notion of marking rows seen.
        self.table.viewport().installEventFilter(self)
        self.size_rows()
        self.size_columns()
        column.addWidget(self.table, 1)
        column.addLayout(self.controls())
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.activated.connect(self.focus_search)
        self.hint = QLabel(HINT, self)
        column.addWidget(self.hint)

    def title_strip(self) -> QWidget:
        """Lay out the top strip: the window's name, its drag handle, and the close mark."""
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
        # Anchored here so the tray's menu opens under this button, even on desktops that ignore screen coordinates.
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
        self.closed_chip = QPushButton("Show closed", self)
        self.closed_chip.setCheckable(True)
        self.closed_chip.toggled.connect(self.set_show_closed)
        strip.addWidget(self.closed_chip)
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
        """Fit the columns exactly to the table, so none is left empty and none needs sideways scrolling.

        Leftover width fills that column first; others then share any further shrink, unless the user widened one.

        :param shrink_all: whether the other columns may also be squeezed
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
        """Remember a column's width after the user drags it, and let the filling column absorb the difference.

        Widths this code sets, or set while hidden, are ignored; a filling-column drag still counts as a resize.
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

        Seen status is the only thing that dims a row, since age already has its own scale in the date column. The
        date colour does not dim, since showing age is its purpose.
        """
        ground = self.table.palette().base().color().name()
        highlight = blend(self.table.palette().highlight().color().name(), ground, HIGHLIGHT_STRENGTH)
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
            if entry.url == self.highlighted_url:
                wash = highlight
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
        """Highlight the row that was clicked.

        :param row: which row was clicked
        """
        self.highlighted_url = self.entries[row].url if 0 <= row < len(self.entries) else None
        self.paint()

    def on_cell_double_clicked(self, row: int, _column: int) -> None:
        """Open the row that was double-clicked on GitHub, and put the window away.

        :param row: which row was double-clicked
        """
        if 0 <= row < len(self.entries) and self.entries[row].url:
            self.open(self.entries[row].url)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Mark a row seen when right-clicked and fit the columns on resize; every other event is the table's own.

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
        self.hint.setText("Refreshing. The window updates once GitHub answers.")
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
        self.hint.setText("Up to date." if succeeded else "Could not reach GitHub. Showing the last known data.")

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
        """Return how wide the window should open.

        Uses the width last dragged to, if any, clamped to this screen in case it was dragged wider on a bigger one.

        :param usable: how much of the screen a window may use
        """
        dragged = self.remembered(WIDTH_KEY)
        wanted = self.characters(dragged) if dragged else self.preferred_width(usable)
        # The bottom controls set their own minimum width; computed here so it is not mistaken for a user resize.
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
        least = max(MINIMUM_HEIGHT, self.minimumSizeHint().height())
        return max(min(around + self.table_height(), tallest), least)

    def settle_layout(self) -> None:
        """Force the layout to compute its sizes now, instead of waiting for the next event-loop pass.

        The minimum size updates only when the layout runs, so placing the window before that reads as a resize.
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
        """Show the window beside a click, sized to its current rows.

        Height follows the row count, so a quiet day gets a small window rather than a tall, empty one.

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
        """Re-fit the window's height to its current rows, growing or shrinking from the top edge.

        The bottom edge stays fixed, since growing downward would push rows off the screen; it grows upward instead.

        :param resize_width: whether to also re-fit the width, needed when the text has been zoomed
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
        """Show the window at a click, or hide it if already shown.

        Clicking the tray icon while open hides the window, via focus loss, before this method sees the click.
        Reopening in response would make that click a no-op, so a click soon after dismissal counts as its cause.

        :param spot: where on screen the click was
        """
        if self.isVisible():
            self.hide()
            return
        if self.dismissed_at is not None and time.monotonic() - self.dismissed_at < TOGGLE_WITHIN_SECONDS:
            logger.debug("this click just dismissed the window; ignoring it")
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
        """Remember the width the user dragged to; widths this code set are not worth keeping.

        :param event: the resize
        """
        super().resizeEvent(event)
        width = event.size().width()
        # A width equal to the window's minimum was the toolkit enforcing its floor as text grew, not a resize.
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
            logger.debug("this desktop will not move the window for the application")

    def start_system_resize(self, edges: Qt.Edge) -> None:
        """Hand the desktop a resize of the window by some of its edges.

        :param edges: which edges are being dragged
        """
        handle = self.windowHandle()
        if handle is not None and handle.startSystemResize(edges):
            self.desktop_dragging = True
        else:
            logger.debug("this desktop will not resize the window for the application")

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

    def hideEvent(self, event: QHideEvent) -> None:
        """Drop the row highlight as the window goes away, so it does not come back with a stale one.

        :param event: the hiding
        """
        self.highlighted_url = None
        super().hideEvent(event)

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
        """Hide the window when focus moves elsewhere, once it has settled after appearing.

        A frameless window has no other way to detect a click elsewhere, since any click takes focus. A loss in
        the first moments, or during a desktop-driven drag, is ignored, since some desktops hold activation throughout.

        :param event: any event the window receives
        """
        if event.type() == QEvent.Type.WindowActivate:
            # Activation returns on the first click after such a drag; losing it again after is the user's doing.
            self.desktop_dragging = False
        # Visibility is asked first: events arrive while the window is still being built, before it has a show time.
        deactivated = event.type() == QEvent.Type.WindowDeactivate and self.isVisible()
        # A menu of this application's own, popped up from the window, is not somebody clicking elsewhere.
        if deactivated and self.settled() and not self.desktop_dragging and QApplication.activePopupWidget() is None:
            logger.debug("focus moved elsewhere; hiding the window")
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
