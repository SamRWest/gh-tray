"""The inks windows draw with, in a dark and a light set, following whichever theme the desktop is set to.

The toolkit paints the windows themselves in the desktop's own colours. This module holds the colour for what a row
means: the reds and ambers of the Change column, the hue dealt to a name, the scale a date is drawn on, and the wash
behind a finished pull request. Each has a dark and a light form, since a red that reads on near-black is lost on
white.

Inks are named for what they mean rather than what they look like, so the same name gives a pale red on white and a
bright one on dark without the caller needing to know which.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from platformdirs import user_data_path

from . import APP_NAME
from .storage import read_json

CONFIG_PATH = user_data_path(APP_NAME, appauthor=False) / "config.json"


@dataclass(frozen=True)
class Palette:
    """The inks for one theme, and the grounds they are checked against.

    The grounds are what a desktop typically paints a window in that theme. A test holds every ink here to a
    contrast of 4.5 to 1 or better on both, so legibility does not depend on a well-adjusted monitor. Windows blend
    towards whatever ground the toolkit actually painted, so these grounds are a reference standard, not a colour
    that gets drawn.
    """

    dark: bool
    background: str
    surface: str
    # The quiet ink, for a status needing no action and a name nobody has. Tinted towards its ground rather than
    # plain grey, which would read as switched off rather than merely quiet.
    muted: str
    # One hue per sort of thing, so a glance down the window tells them apart without reading a word. Bright enough
    # to stay themselves when dimmed for an already-seen row, which a muted colour would not.
    red: str
    orange: str
    amber: str
    green: str
    blue: str
    violet: str
    pink: str
    # The two ends of the date scale: blue for something that just happened, red for something long forgotten. Age
    # reads at a glance rather than as two shades of the same thing.
    fresh: str
    stale: str


# Neutral near-black grounds in the manner of an IDE's high-contrast dark scheme, with the accents kept muted rather
# than neon.
DARK = Palette(
    dark=True,
    background="#1e1f22",
    surface="#2b2d30",
    muted="#9da3ae",
    red="#f86270",
    orange="#e08855",
    amber="#d6b85a",
    green="#73bd79",
    blue="#56a8f5",
    violet="#b189f5",
    pink="#e578c2",
    fresh="#56a8f5",
    stale="#f86270",
)

# The same hues taken dark enough to read on white, held to the same contrast floor.
LIGHT = Palette(
    dark=False,
    background="#ffffff",
    surface="#f2f3f5",
    muted="#575b66",
    red="#c22b41",
    orange="#a45017",
    amber="#7a6011",
    green="#1e7d33",
    blue="#2467c0",
    violet="#7b3fd4",
    pink="#b02c86",
    fresh="#2467c0",
    stale="#c22b41",
)


# What the theme setting may be set to, and what each means.
FOLLOW_DESKTOP, ALWAYS_DARK, ALWAYS_LIGHT = "auto", "dark", "light"
STYLES = (FOLLOW_DESKTOP, ALWAYS_DARK, ALWAYS_LIGHT)


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


def ink(inks: Palette, name: str) -> str:
    """Return the colour a named ink is in a palette.

    Rows carry the names of their inks rather than the colours, so a window can follow the desktop from dark to
    light without rebuilding them.

    :param inks: the palette of the theme being drawn in
    :param name: the ink's name, which is one of the palette's fields
    """
    return getattr(inks, name)


def is_dark() -> bool:
    """Return whether the desktop is set to a dark theme, defaulting to dark when it cannot be told.

    Asks the toolkit, which reads the desktop's setting on every platform but needs the application to exist first.
    Imported here, not at the top, so a command that opens no window never loads it.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Unknown:
        logger.debug("this desktop does not say which theme it is set to, assuming dark")
        return True
    return scheme == Qt.ColorScheme.Dark


def palette(style: str = FOLLOW_DESKTOP) -> Palette:
    """Return the inks to draw with.

    :param style: ``dark`` or ``light`` to insist on one, or ``auto`` to follow whatever the desktop is set to
    """
    if style == ALWAYS_DARK:
        return DARK
    if style == ALWAYS_LIGHT:
        return LIGHT
    return DARK if is_dark() else LIGHT


def chosen_style() -> str:
    """Return the theme the settings ask for, read from the settings file directly.

    The settings module cannot be imported here, since it needs this one. The file is small and read as a window
    comes up or the desktop changes, so reading it again costs nothing.
    """
    stored, _damaged = read_json(CONFIG_PATH)
    asked = stored.get("theme") if isinstance(stored, dict) else None
    return asked if asked in STYLES else FOLLOW_DESKTOP
