"""Starts the toolkit; windows draw in the desktop's colours and follow light/dark via :mod:`theme`."""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from importlib import import_module
from typing import ClassVar, Protocol

from loguru import logger
from PIL import Image
from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QKeyEvent, QPalette, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

from gh_tray.config import LAYOUT_PATH
from gh_tray.theme import ALWAYS_DARK, ALWAYS_LIGHT, DARK

# Where the text zoom is remembered, and how far the text may be taken from the platform's own size, in points.
ZOOM_KEY = "font/zoom"
ZOOM_RANGE = (-4, 16)
# The application remembers the platform's starting font under this name, so zoom is measured from one point.
BASE_FONT_PROPERTY = "base_font"


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
    """Return the application object, starting the toolkit if it has not started yet.

    Closing the last window must not quit, since the tray often has none open and the settings window closes often.
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


def compositing_available() -> bool:
    """Return whether the desktop composites windows, which see-through and rounded ones need.

    Windows, macOS and Wayland always composite. X11 does only with a compositing manager, which owns the
    _NET_WM_CM_S0 selection; without one a translucent window shows black where it should be see-through.
    """
    if QGuiApplication.platformName() != "xcb":
        return True
    return x11_compositor_running()


def x11_compositor_running() -> bool:
    """Return whether an X11 compositing manager owns the first screen, asked of the X library directly."""
    import ctypes
    import ctypes.util

    name = ctypes.util.find_library("X11")
    if not name:
        return False
    x11 = ctypes.CDLL(name)
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XGetSelectionOwner.restype = ctypes.c_ulong
    x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        return x11.XGetSelectionOwner(display, x11.XInternAtom(display, b"_NET_WM_CM_S0", 0)) != 0
    finally:
        x11.XCloseDisplay(display)


# Desktop window manager attributes (Windows 11): dark mode and corner rounding. The blur itself goes through the
# window composition attribute's accent policy, since the newer system backdrop is drawn as a solid colour on
# some machines that accept it.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
# The acrylic's own tint: black at a low alpha, in ABGR, so the window's painted background does the tinting.
ACRYLIC_TINT = 0x20000000
# Where Windows keeps the transparency-effects switch, and the metric that says this is a remote session. With the
# switch off or over a remote session the backdrop is drawn as a solid colour and corners stay square.
PERSONALISE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
SM_REMOTESESSION = 0x1000
# AppKit constants for a visual effect view that blurs what lies behind the window.
NS_VIEW_WIDTH_SIZABLE = 2
NS_VIEW_HEIGHT_SIZABLE = 16
NS_BLENDING_BEHIND_WINDOW = 0
NS_MATERIAL_POPOVER = 6
NS_STATE_ACTIVE = 1
NS_WINDOW_BELOW = -1
# X11 property KWin reads to blur behind a window, and the atom types the X library expects.
KDE_BLUR_PROPERTY = b"_KDE_NET_WM_BLUR_BEHIND_REGION"
XA_CARDINAL = 6
PROP_MODE_REPLACE = 0


class Hideable(Protocol):
    """What a macOS view offers for hiding and showing itself."""

    def setHidden_(self, hidden: bool) -> None:
        """Hide or show the view."""


@dataclass
class Blur:
    """Which desktop blurs behind a window, and what it needs to switch the blur off and on again."""

    kind: str = ""
    effect: Hideable | None = None

    def __bool__(self) -> bool:
        """Whether the desktop blurs behind the window at all."""
        return bool(self.kind)


def blur_behind(window: QWidget, radius: int) -> Blur:
    """Ask the desktop to blur what lies behind a see-through window, where it can.

    Blur is decoration, so a desktop that refuses it is logged and the window goes on without it.

    :param window: a top-level window drawn on a see-through background
    :param radius: how round the window's corners are, for a desktop that clips the blur to them
    :return: which desktop did it, ``dwm``, ``cocoa`` or ``kde``, or nothing for none
    """
    # The native handle means something only on the desktop's own platform plugin: on the offscreen one used by
    # the tests it is not a window at all, and handing it to the desktop crashes the process.
    platform = QGuiApplication.platformName()
    try:
        if sys.platform == "win32" and platform == "windows":
            return Blur("dwm") if windows_acrylic(window, True) else Blur()
        if sys.platform == "darwin" and platform == "cocoa":
            effect = macos_vibrancy(window, radius)
            return Blur("cocoa", effect) if effect is not None else Blur()
        if platform == "xcb":
            return Blur("kde") if kde_blur(window, True) else Blur()
    except Exception as error:  # any failure here is the desktop's, and the window must still open
        logger.debug("blur behind the window refused: {}", error)
    return Blur()


def show_blur(window: QWidget, blur: Blur, shown: bool) -> None:
    """Switch a desktop's blur behind a window off or back on.

    :param window: the window the blur was given to
    :param blur: what :func:`blur_behind` returned for it
    :param shown: whether the blur should be drawn
    """
    try:
        if blur.kind == "dwm":
            windows_acrylic(window, shown)
        elif blur.kind == "cocoa" and blur.effect is not None:
            blur.effect.setHidden_(not shown)
        elif blur.kind == "kde":
            kde_blur(window, shown)
    except Exception as error:  # the blur is decoration; the window must go on
        logger.debug("could not switch the blur {}: {}", "on" if shown else "off", error)


def windows_acrylic(window: QWidget, shown: bool) -> bool:
    """Blur behind a window with the acrylic accent, and have Windows 11 round its corners.

    :param window: the window, whose native handle is created here if it was not yet
    :param shown: whether the blur should be drawn
    """
    if sys.platform != "win32":
        return False
    import ctypes

    if ctypes.windll.user32.GetSystemMetrics(SM_REMOTESESSION):
        logger.debug("no blur: a remote session does not draw it")
        return False
    if not windows_transparency_effects():
        logger.debug("no blur: transparency effects are off")
        return False

    class AccentPolicy(ctypes.Structure):
        _fields_: ClassVar = [
            ("state", ctypes.c_int),
            ("flags", ctypes.c_int),
            ("colour", ctypes.c_uint),
            ("animation", ctypes.c_int),
        ]

    class CompositionAttribute(ctypes.Structure):
        _fields_: ClassVar = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]

    handle = int(window.winId())
    dwm = ctypes.windll.dwmapi

    def put(attribute: int, value: int) -> int:
        holder = ctypes.c_int(value)
        return dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(handle), attribute, ctypes.byref(holder), ctypes.sizeof(holder)
        )

    put(DWMWA_USE_IMMERSIVE_DARK_MODE, int(window.palette().window().color().lightness() < 128))
    put(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    policy = AccentPolicy(ACCENT_ENABLE_ACRYLICBLURBEHIND if shown else ACCENT_DISABLED, 0, ACRYLIC_TINT, 0)
    data = CompositionAttribute(
        WCA_ACCENT_POLICY, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p), ctypes.sizeof(policy)
    )
    user32 = ctypes.windll.user32
    user32.SetWindowCompositionAttribute.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return bool(user32.SetWindowCompositionAttribute(ctypes.c_void_p(handle), ctypes.byref(data)))


def windows_transparency_effects() -> bool:
    """Return whether Windows is set to draw transparency effects; a missing setting counts as on."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALISE_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "EnableTransparency")
    except OSError:
        return True
    return bool(value)


def macos_vibrancy(window: QWidget, radius: int) -> Hideable | None:
    """Put a visual effect view behind a window's content, which macOS blurs what lies behind the window into.

    :param window: the window, whose native view is created here if it was not yet
    :param radius: how round the effect's corners are, matching the window's own
    :return: the effect view, kept so it can be hidden, or None where there is no native window
    """
    if sys.platform != "darwin":
        return None
    objc = import_module("objc")
    appkit = import_module("AppKit")
    view = objc.objc_object(c_void_p=int(window.winId()))
    native = view.window()
    if native is None:
        return None
    effect = appkit.NSVisualEffectView.alloc().initWithFrame_(view.bounds())
    effect.setAutoresizingMask_(NS_VIEW_WIDTH_SIZABLE | NS_VIEW_HEIGHT_SIZABLE)
    effect.setBlendingMode_(NS_BLENDING_BEHIND_WINDOW)
    effect.setMaterial_(NS_MATERIAL_POPOVER)
    effect.setState_(NS_STATE_ACTIVE)
    effect.setWantsLayer_(True)
    effect.layer().setCornerRadius_(radius)
    effect.layer().setMasksToBounds_(True)
    view.addSubview_positioned_relativeTo_(effect, NS_WINDOW_BELOW, None)
    native.setOpaque_(False)
    native.setBackgroundColor_(appkit.NSColor.clearColor())
    return effect


def kde_blur(window: QWidget, shown: bool) -> bool:
    """Mark a window for KWin to blur behind, on a KDE desktop running under X11, or take the mark off again.

    Other window managers ignore the mark, so it is set only where KDE says it is the desktop.

    :param window: the window, whose X window is created here if it was not yet
    :param shown: whether the blur should be drawn
    """
    import ctypes
    import ctypes.util
    import os

    if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return False
    name = ctypes.util.find_library("X11")
    if not name:
        return False
    x11 = ctypes.CDLL(name)
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XChangeProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    x11.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        atom = x11.XInternAtom(display, KDE_BLUR_PROPERTY, 0)
        if shown:
            # An empty region means the whole window.
            x11.XChangeProperty(display, int(window.winId()), atom, XA_CARDINAL, 32, PROP_MODE_REPLACE, None, 0)
        else:
            x11.XDeleteProperty(display, int(window.winId()), atom)
        x11.XFlush(display)
    finally:
        x11.XCloseDisplay(display)
    return True


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

    Installed on the application so it sees the wheel first; only the font changes, widgets take sizes from it.
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
        """Set the application's font to the platform's own size, adjusted by the remembered number of zoom steps."""
        font = QFont(base_font())
        if font.pointSize() > 0:
            font.setPointSize(max(1, font.pointSize() + self.steps))
        else:
            font.setPixelSize(max(1, font.pixelSize() + self.steps))
        app = application()
        app.setFont(font)
        # Open windows get the font directly here; windows built later take it from the application automatically.
        for shown in app.topLevelWidgets():
            shown.setFont(font)

    def stop(self) -> None:
        """Stop watching the wheel, leaving the text at whatever size it is."""
        application().removeEventFilter(self)


# STYLE_THAT_STAYS_LIGHT is what Windows before 11 starts with.
STYLE_THAT_STAYS_LIGHT = "windowsvista"
SCHEME_FOLLOWING_STYLE = "Fusion"
STARTING_STYLE_PROPERTY = "starting_style"


def wanted_scheme(style: str) -> Qt.ColorScheme:
    """Return the scheme to draw the windows in: the one insisted on, else the desktop's, else dark.

    A desktop that cannot report its scheme (Windows Server, for one) gets dark, matching the row inks.
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
    """Draws windows dark or light as settings say, or as the desktop is; Windows before 11 needs a style switch.

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
    # A platform that ignores a requested scheme is given a palette instead, and the style's own palette for light.
    if QGuiApplication.styleHints().colorScheme() != scheme:
        app.setPalette(dark_palette() if scheme == Qt.ColorScheme.Dark else app.style().standardPalette())
    logger.debug("theme setting {!r}: drawing {} in the {} style", style, scheme.name, app.style().name())
