"""The inks the windows draw with, in a dark and a light set, following whichever theme the desktop is set to.

The toolkit paints the windows themselves in the desktop's own colours. What is kept here is the colour a row is
given for what it is: the reds and ambers of the Change column, the hue a name is dealt, the scale a date is drawn
on, and the wash a finished pull request sits on. Each comes in a dark and a light form, since a red that reads on
a near-black ground is lost on white.

Colours are named for what they mean rather than for what they look like, so the same name can be a pale red on a
white background and a bright one on a dark background without any caller having to know which it got.
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

    The grounds are what a desktop typically paints a window in that theme. Every ink here reads at 4.5 to 1 or
    better on both, which a test holds it to, so nothing depends on a well-adjusted monitor. The windows blend
    towards the colour the toolkit actually painted them, so the grounds are a standard rather than something drawn.
    """

    dark: bool
    background: str
    surface: str
    # The quiet ink, for a status nobody need act on and a name nobody has. Tinted towards its ground rather than
    # plain grey, which would read as switched off rather than merely quiet.
    muted: str
    # One hue per sort of thing, so a glance down the window tells them apart without reading a word. They are
    # bright enough to stay themselves when dimmed for a row already seen, which a muted colour does not.
    red: str
    orange: str
    amber: str
    green: str
    blue: str
    violet: str
    pink: str
    # The two ends of the scale a date is drawn on: blue for something that just happened, through to red for
    # something long forgotten, so age reads at a glance rather than as two shades of the same thing.
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
    light without the rows having to be built again.

    :param inks: the palette of the theme being drawn in
    :param name: the ink's name, which is one of the palette's fields
    """
    return getattr(inks, name)


def is_dark() -> bool:
    """Return whether the desktop is set to a dark theme, defaulting to dark when it cannot be told.

    Asked of the toolkit, which reads the desktop's setting on every platform and needs the application to exist
    first. It is imported here rather than at the top, so a command that opens no window never loads it.
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
