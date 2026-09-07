"""The inks windows draw with, in a dark and a light set, following the desktop's theme.

Inks are named for meaning, not appearance, so one name gives a pale red on white and a bright red on dark.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from platformdirs import user_data_path

from gh_tray import APP_NAME
from gh_tray.storage import read_json

CONFIG_PATH = user_data_path(APP_NAME, appauthor=False) / "config.json"


@dataclass(frozen=True)
class Palette:
    """The inks for one theme, and the grounds they are checked against.

    The grounds are a fixed reference: a test holds every ink to 4.5:1 contrast or better against both, though a
    window actually blends towards whichever ground the toolkit paints, so the palette is only a reference.
    """

    dark: bool
    background: str
    surface: str
    # The quiet ink, for no-action status; tinted towards its ground, not plain grey, which would read as switched off.
    muted: str
    # One hue per sort of thing, readable at a glance; bright enough to stay itself when dimmed for a seen row.
    red: str
    orange: str
    amber: str
    green: str
    blue: str
    violet: str
    pink: str
    # The two ends of the date scale: blue for recent, red for long forgotten, so age reads at a glance.
    fresh: str
    stale: str


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


def wash(colour: str, strength: float) -> str:
    """Give a colour an alpha, for a background laid over whatever the window shows through.

    A blend into the ground paints a solid colour, which hides a see-through window's background. A colour that
    carries its own alpha composes over it instead, and over a solid ground it comes out the same as the blend.

    :param colour: the colour to wash with, as ``#rrggbb``
    :param strength: how much of the colour shows, where one is solid and zero is nothing
    :return: the colour as ``#aarrggbb``, which the toolkit reads alpha first
    """
    return f"#{round(255 * strength):02x}{colour[1:]}"


def ink(inks: Palette, name: str) -> str:
    """Return the colour a named ink is in a palette.

    :param inks: the palette of the theme being drawn in
    :param name: the ink's name, which is one of the palette's fields
    """
    return getattr(inks, name)


def is_dark() -> bool:
    """Return whether the desktop is set to a dark theme, defaulting to dark when it cannot be told.

    Imported here rather than at the top, so a windowless command asking for the toolkit is not forced to load it.
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

    The settings module cannot be imported here, since it needs this one; re-reading the small file costs nothing.
    """
    stored, _damaged = read_json(CONFIG_PATH)
    asked = stored.get("theme") if isinstance(stored, dict) else None
    return asked if asked in STYLES else FOLLOW_DESKTOP
