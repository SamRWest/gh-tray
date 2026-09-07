"""The changes window: a small tool window listing what needs the user's attention.

The tray builds the window once and hides rather than closes it, so reopening is instant. It is frameless, so
this module draws its own title strip, resize edges, close mark and menu button, with the desktop handling
drags and resizes.
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
from .config import APP_ICON_PATH, BLUR_KEY, OPACITY_KEY, default_opacity, load_config
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
from .toolkit import Blur, blur_behind, compositing_available, layout_store, show_blur

EDGE_MARGIN = 12
# Clears the pointer, and the taskbar for a tray-icon click.
POINTER_OFFSET = 16
# Extra gap above the pointer, so a click near the screen bottom still clears it.
POINTER_GAP = 24
MINIMUM_WIDTH = 480
MINIMUM_HEIGHT = 140
# Extra width for the edges, scrollbar and table padding, so no column starts out cut off.
WIDTH_ALLOWANCE = 70
# Caps width so a long repository name at large text cannot fill the screen; columns shrink instead.
WIDEST_SHARE_OF_SCREEN = 0.9
FILLING_COLUMN = next(name for name, _heading, _width, fills in COLUMNS if fills)
SHORTEST_COLUMN = 4
# Beyond this share of the screen height, rows scroll instead of the window growing further.
TALLEST_SHARE_OF_SCREEN = 0.55
ROW_PADDING = 10
# Width of the resize border and margin, so a press anywhere in the margin grabs an edge.
GRIP = 8
# The see-through margin around a rounded window that holds its shadow, and how round the corners are.
SHADOW = 12
CORNER = 10
# How dark the shadow is at the window's edge; it fades to nothing across the margin.
SHADOW_ALPHA = 90
# A focus loss before this is the window arriving, not a click elsewhere; otherwise it hides on every showing.
FOCUS_SETTLE_SECONDS = 0.3
# How soon after losing focus a tray click counts as the dismissal, not a fresh request to reopen.
TOGGLE_WITHIN_SECONDS = 0.5

DATE_COLUMN = "when"
STATUS_COLUMN = "status"
NAMED_COLUMNS = ("org", "repo", "author", "who")

# Layout-store keys for remembered widths, keyed by column name. Widths are stored in characters of the font,
# not pixels, so they still match after a display-scale or zoom change.
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
        # Closed pull requests start hidden: they are done, and this window lists what is not.
        self.show_closed = False
        self.sort_column = DEFAULT_SORT
        self.newest_first = True
        self.search_text = ""
        self.inks: Palette = palette(chosen_style())
        self.layout_store = layout if layout is not None else layout_store()
        self.placed_width = 0
        self.awaiting_poll = False
        self.fitting = False
        # None rather than a sentinel time: a monotonic clock has no fixed zero, so no number safely means "never".
        self.shown_at = 0.0
        self.dismissed_at: float | None = None
        # The row last clicked, by URL so it survives sorting and refilling.
        self.highlighted_url: str | None = None
        # Shown beside the settings to preview a change, without the focus, and put away when they close.
        self.previewing = False
        # Some desktops, GNOME included, hold the window's activation for a whole drag, so losing it then is not
        # a click elsewhere.
        self.desktop_dragging = False
        # See-through and rounded only where the desktop composites; elsewhere the window stays square and solid.
        self.translucent = compositing_available()
        if self.translucent:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Where the desktop can blur behind the window it also shadows it, so the painted shadow is left out.
        self.blur = blur_behind(self, CORNER) if self.translucent else Blur()
        self.shadow = SHADOW if self.translucent and not self.blur else 0
        self.blur_wanted = True
        self.opacity = 0
        self.wanted_opacity: int | None = None
        settings = load_config()
        self.set_blur(bool(settings.get(BLUR_KEY, True)))
        self.set_opacity(settings.get(OPACITY_KEY))
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
        margin = self.frame_margin()
        column.setContentsMargins(margin, margin, margin, margin)
        column.addWidget(self.title_strip())
        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels([heading for _key, heading, _width, _stretches in COLUMNS])
        self.table.verticalHeader().hide()
        if self.translucent:
            # The viewport paints no background of its own, so the window's see-through one shows through the rows.
            self.table.viewport().setAutoFillBackground(False)
        # Selection and focus stay off: the desktop style frames each selected or focused cell, showing as a bar
        # in every cell. The clicked row is highlighted by painting it instead.
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
        # Catches right clicks before the table acts on them; the table has no notion of marking rows seen.
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
        """Fit the columns exactly to the table: the filling column takes any leftover width, and shrinks first.

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
        """Remember a column's width after the user drags it, ignoring widths this code sets or sets while hidden."""
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
        """Colour every cell; a seen row dims except its date, which keeps its own age scale."""
        ground = self.ground().name()
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
        wanted += 2 * (self.frame_margin() - GRIP)
        return min(wanted, int(usable.width() * WIDEST_SHARE_OF_SCREEN))

    def wanted_width(self, usable: QRect) -> int:
        """Return how wide the window should open: the width last dragged to, clamped to this screen.

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
        """Force layout to compute sizes now; the minimum size is stale until this runs, else that reads as a resize."""
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
        """Show the window beside a click, sized to its current rows so a quiet day gets a small window.

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
        self.bring_up(QRect(left, top, width, height))
        logger.debug(
            "showing {} rows in the {} theme at {}",
            len(self.entries),
            "dark" if self.inks.dark else "light",
            self.geometry(),
        )

    def refit(self, resize_width: bool = False) -> None:
        """Re-fit the window's height to its current rows, growing or shrinking from the top edge.

        The bottom edge stays fixed, since growing downward would push rows off the screen.
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

    def show_below(self, other: QRect) -> None:
        """Show the window under another one without taking the focus, so a settings change is seen at once.

        :param other: where the other window is, on screen
        """
        self.reload()
        self.settle_layout()
        usable = self.usable_screen(other.center())
        width, height = self.wanted_width(usable), self.wanted_height(usable)
        left = max(usable.left() + EDGE_MARGIN, min(other.left(), usable.right() - width - EDGE_MARGIN))
        top = other.bottom() + EDGE_MARGIN
        if top + height > usable.bottom() - EDGE_MARGIN:
            top = max(usable.top() + EDGE_MARGIN, other.top() - height - EDGE_MARGIN)
        self.previewing = True
        self.bring_up(QRect(left, top, width, height), activate=False)

    def bring_up(self, geometry: QRect, activate: bool = True) -> None:
        """Place the window and show it, fresh for the dismissal timing.

        :param geometry: where the window goes
        :param activate: whether it takes the focus, which a preview beside another window must not
        """
        self.place(geometry)
        self.shown_at = time.monotonic()
        self.dismissed_at = None
        self.desktop_dragging = False
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        self.show()
        self.raise_()
        if activate:
            self.activateWindow()

    def toggle(self, spot: QPoint) -> None:
        """Show the window at a click, or hide it if already shown.

        A tray click while open hides the window, via focus loss, before this method sees the click, so a click
        soon after dismissal counts as that click's cause rather than a fresh request to reopen.
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
        grip = self.frame_margin()
        if spot.x() < grip:
            edges |= Qt.Edge.LeftEdge
        if spot.x() >= self.width() - grip:
            edges |= Qt.Edge.RightEdge
        if spot.y() < grip:
            edges |= Qt.Edge.TopEdge
        if spot.y() >= self.height() - grip:
            edges |= Qt.Edge.BottomEdge
        return edges

    def frame_margin(self) -> int:
        """Return the margin around the contents: the grip, plus the shadow where one is painted."""
        return GRIP + self.shadow

    def ground(self) -> QColor:
        """Return the colour the rows sit on, which is the window's own where the viewport paints none."""
        return self.palette().window().color() if self.translucent else self.table.palette().base().color()

    def set_opacity(self, percent: int | None) -> None:
        """Make the background this solid, where the desktop can draw a see-through window at all.

        :param percent: how solid the background is, up to 100, or None for the default given the blur
        """
        self.wanted_opacity = int(percent) if percent is not None else None
        self.opacity = self.wanted_opacity if self.wanted_opacity is not None else default_opacity(self.blurred())
        self.update()

    def blurred(self) -> bool:
        """Return whether the desktop is blurring behind the window right now."""
        return bool(self.blur) and self.blur_wanted

    def set_blur(self, wanted: bool) -> None:
        """Switch the desktop's blur behind the window off or on, where there is one, keeping the default opacity apt.

        :param wanted: whether the blur should be drawn
        """
        self.blur_wanted = wanted
        if self.blur:
            show_blur(self, self.blur, wanted)
        self.set_opacity(self.wanted_opacity)

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
        self.previewing = False
        super().hideEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the background: rounded, see-through and shadowed where the desktop composites, else a border.

        :param event: what needs painting
        """
        super().paintEvent(event)
        painter = QPainter(self)
        if not self.translucent:
            painter.setPen(self.palette().mid().color())
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.rect().adjusted(self.shadow, self.shadow, -self.shadow - 1, -self.shadow - 1)
        shade = QColor(0, 0, 0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for step in range(self.shadow, 0, -1):
            shade.setAlpha(int(SHADOW_ALPHA * (1 - step / SHADOW) ** 2))
            painter.setPen(shade)
            painter.drawRoundedRect(inner.adjusted(-step, -step, step, step), CORNER + step, CORNER + step)
        ground = self.ground()
        ground.setAlpha(round(255 * self.opacity / 100))
        painter.setBrush(ground)
        if self.blur.kind == "dwm":
            # Windows rounds and borders the window itself, and clips the tint to its corners.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(self.rect())
        else:
            painter.setPen(self.palette().mid().color())
            painter.drawRoundedRect(inner, CORNER, CORNER)
        painter.end()

    def event(self, event: QEvent) -> bool:
        """Hide the window when focus moves elsewhere, once it has settled after appearing.

        A frameless window has no other way to detect a click elsewhere, since any click takes focus. A loss in
        the first moments, or during a desktop-driven drag (some desktops hold activation throughout), is ignored.
        :param event: any event the window receives
        """
        if event.type() == QEvent.Type.WindowActivate:
            self.desktop_dragging = False
        # isVisible() first: events arrive while the window is still being built, before it has a show time.
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
