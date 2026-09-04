"""Starting the toolkit, and the little of it the tray and both windows share.

The toolkit is the user interface library. It draws the windows in the desktop's own colours and follows the desktop
between light and dark, which is why the windows carry no palette of their own beyond the inks in :mod:`theme`.
"""

from __future__ import annotations

import io
import sys

from loguru import logger
from PIL import Image
from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QKeyEvent, QPalette, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication

from .config import LAYOUT_PATH
from .theme import ALWAYS_DARK, ALWAYS_LIGHT, DARK

# Where the text zoom is remembered, and how far the text may be taken from the platform's own size, in points.
ZOOM_KEY = "font/zoom"
ZOOM_RANGE = (-4, 16)
# The application remembers the font the platform gave it under this name, so that every zoom in one process is
# measured from the same starting point however many times one is applied.
BASE_FONT_PROPERTY = "base_font"


# The log level each of the toolkit's own message kinds deserves. Anything unlisted is detail.
TOOLKIT_LEVELS = {
    QtMsgType.QtInfoMsg: "INFO",
    QtMsgType.QtWarningMsg: "WARNING",
    QtMsgType.QtCriticalMsg: "ERROR",
    QtMsgType.QtFatalMsg: "CRITICAL",
}


def route_toolkit_messages() -> None:
    """Send the toolkit's own messages to the log rather than to standard error, where nobody reads them."""
    qInstallMessageHandler(
        lambda kind, _context, text: logger.log(TOOLKIT_LEVELS.get(kind, "DEBUG"), "toolkit: {}", text)
    )


def application() -> QApplication:
    """Return the application object, starting the toolkit if nothing has yet.

    Closing the last window must not quit: the tray has no window up most of the time, and the settings window is
    closed far more often than the tray is.
    """
    running = QApplication.instance()
    if isinstance(running, QApplication):
        return running
    started = QApplication(sys.argv)
    started.setQuitOnLastWindowClosed(False)
    return started


def icon_from(picture: Image.Image) -> QIcon:
    """Turn a drawn picture into an icon the toolkit can show, through a portable image format held in memory.

    :param picture: the picture, as the drawing library produced it
    """
    held = io.BytesIO()
    picture.save(held, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(held.getvalue())
    return QIcon(pixmap)


def layout_store() -> QSettings:
    """Return the store of dragged widths and the text zoom, kept beside the application's other state files."""
    return QSettings(str(LAYOUT_PATH), QSettings.Format.IniFormat)


def base_font() -> QFont:
    """Return the font the platform gave the application, as it was before any zoom."""
    app = application()
    kept = app.property(BASE_FONT_PROPERTY)
    if not isinstance(kept, QFont):
        kept = QFont(app.font())
        app.setProperty(BASE_FONT_PROPERTY, kept)
    return kept


class FontZoom(QObject):
    """Ctrl and the mouse wheel change the size of every window's text, and the change is remembered.

    Installed on the application, so it sees the wheel before whichever widget is under the pointer does, and a
    table, a spin box and a button all zoom alike. Only the font changes: the widgets take their sizes from it, so
    rows, headings and buttons follow. Ctrl and 0 put the text back to the platform's own size.
    """

    changed = Signal()

    def __init__(self, store: QSettings) -> None:
        """Take up the remembered zoom and start watching the wheel.

        :param store: where the zoom is remembered
        """
        super().__init__()
        self.store = store
        stored = store.value(ZOOM_KEY, 0, int)
        self.steps = self.clamped(stored if isinstance(stored, int) else 0)
        self.apply()
        application().installEventFilter(self)

    @staticmethod
    def clamped(steps: int) -> int:
        """Return a zoom kept within the range the text stays readable in.

        :param steps: the zoom asked for
        """
        return min(max(ZOOM_RANGE[0], steps), ZOOM_RANGE[1])

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Zoom on Ctrl and the wheel, reset on Ctrl and 0, and leave every other event alone.

        :param watched: whatever the event was sent to
        :param event: the event
        """
        control = Qt.KeyboardModifier.ControlModifier
        if isinstance(event, QWheelEvent) and event.type() == QEvent.Type.Wheel and event.modifiers() & control:
            self.step(1 if event.angleDelta().y() > 0 else -1)
            return True
        if (
            isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.modifiers() & control
            and event.key() == Qt.Key.Key_0
        ):
            self.reset()
            return True
        return super().eventFilter(watched, event)

    def step(self, by: int) -> None:
        """Take the text a number of points larger or smaller, remember it, and say so.

        :param by: how many points, negative to shrink
        """
        wanted = self.clamped(self.steps + by)
        if wanted == self.steps:
            return
        self.steps = wanted
        self.store.setValue(ZOOM_KEY, wanted)
        logger.debug("text zoomed to {:+d}", wanted)
        self.apply()
        self.changed.emit()

    def reset(self) -> None:
        """Put the text back to the platform's own size."""
        self.step(-self.steps)

    def apply(self) -> None:
        """Set the application's font to the platform's own, taken the remembered number of steps larger or smaller."""
        font = QFont(base_font())
        if font.pointSize() > 0:
            font.setPointSize(max(1, font.pointSize() + self.steps))
        else:
            font.setPixelSize(max(1, font.pixelSize() + self.steps))
        app = application()
        app.setFont(font)
        # The application's font reaches the windows already up in their own time, and sizes taken from them straight
        # afterwards would be a step behind, so each is handed the font here and now. Windows built later take it
        # from the application.
        for shown in app.topLevelWidgets():
            shown.setFont(font)

    def stop(self) -> None:
        """Stop watching the wheel, leaving the text at whatever size it is."""
        application().removeEventFilter(self)


# The widget style that draws light whatever scheme it is given, which is the one Windows before 11 starts with, and
# the style that draws whichever it is given everywhere.
STYLE_THAT_STAYS_LIGHT = "windowsvista"
SCHEME_FOLLOWING_STYLE = "Fusion"
# The application remembers the widget style it started with under this name, so that light can go back to it.
STARTING_STYLE_PROPERTY = "starting_style"


def wanted_scheme(style: str) -> Qt.ColorScheme:
    """Return the scheme the windows should be drawn in: the one insisted on, or the desktop's, or dark.

    Dark is what a desktop that cannot be told gets, as it is for the row inks, so the two agree. A server edition of
    Windows is one such desktop: it lacks the setting the toolkit reads.

    :param style: ``dark``, ``light``, or anything else to follow the desktop
    """
    insisted = {ALWAYS_DARK: Qt.ColorScheme.Dark, ALWAYS_LIGHT: Qt.ColorScheme.Light}.get(style)
    if insisted is not None:
        return insisted
    hints = QGuiApplication.styleHints()
    hints.setColorScheme(Qt.ColorScheme.Unknown)
    return Qt.ColorScheme.Dark if hints.colorScheme() == Qt.ColorScheme.Unknown else hints.colorScheme()


def dark_palette() -> QPalette:
    """Return a dark palette for the platforms that cannot be asked for one, in the same greys as the dark inks."""
    ground, surface, text = QColor(DARK.surface), QColor(DARK.background), QColor("#ced0d6")
    quiet, highlight = QColor(DARK.muted), QColor("#2e436e")
    role = QPalette.ColorRole
    palette = QPalette()
    for where, colour in (
        (role.Window, ground),
        (role.WindowText, text),
        (role.Base, surface),
        (role.AlternateBase, ground),
        (role.Text, text),
        (role.Button, ground),
        (role.ButtonText, text),
        (role.ToolTipBase, ground),
        (role.ToolTipText, text),
        (role.Highlight, highlight),
        (role.HighlightedText, text),
        (role.PlaceholderText, quiet),
    ):
        palette.setColor(where, colour)
    for where in (role.Text, role.WindowText, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, where, quiet)
    return palette


def follow_theme_setting(style: str) -> None:
    """Draw the windows dark or light as the settings say, or as the desktop is, taking a style that can if need be.

    The widget style Windows starts with before 11 draws light whatever it is told, so where dark is wanted under it
    the windows take the toolkit's own style instead, and go back when light is wanted again.

    :param style: ``dark``, ``light``, or anything else to follow the desktop
    """
    app = application()
    if not isinstance(app.property(STARTING_STYLE_PROPERTY), str):
        app.setProperty(STARTING_STYLE_PROPERTY, app.style().name())
    scheme = wanted_scheme(style)
    QGuiApplication.styleHints().setColorScheme(scheme)
    starting = str(app.property(STARTING_STYLE_PROPERTY))
    if scheme == Qt.ColorScheme.Dark and starting == STYLE_THAT_STAYS_LIGHT:
        app.setStyle(SCHEME_FOLLOWING_STYLE)
    elif app.style().name() != starting:
        app.setStyle(starting)
    # A platform that will not be told a scheme is handed a palette instead, and given the style's own back for light.
    if QGuiApplication.styleHints().colorScheme() != scheme:
        app.setPalette(dark_palette() if scheme == Qt.ColorScheme.Dark else app.style().standardPalette())
    logger.debug("theme setting {!r}: drawing {} in the {} style", style, scheme.name, app.style().name())
