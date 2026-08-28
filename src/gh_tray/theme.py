"""The colours the windows are drawn in, following whichever theme the desktop is set to.

The theme is read once, when a window's process starts. Both windows are launched afresh each time they are opened,
so they always match a theme the user changed since last time without anything having to watch for it.

Colours are named for what they mean rather than for what they look like, so the same name can be a pale red on a
white background and a bright one on a dark background without any caller having to know which it got.
"""

from __future__ import annotations

from dataclasses import dataclass

import darkdetect
from loguru import logger
from platformdirs import user_data_path

from . import APP_NAME
from .storage import read_json

CONFIG_PATH = user_data_path(APP_NAME, appauthor=False) / "config.json"


@dataclass(frozen=True)
class Palette:
    """Every colour a window draws with.

    No colour here is a plain grey. Text drawn in one reads as switched off rather than merely quiet, so the quiet
    inks are tinted towards the surface they sit on and stay part of the same picture.
    """

    dark: bool
    background: str
    surface: str
    border: str
    heading: str
    text: str
    muted: str
    link: str
    hover: str
    selection: str
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


# Neutral near-black greys in the manner of an IDE's high-contrast dark scheme, with the accents kept muted rather
# than neon. Every ink here reads at 4.5 to 1 or better against both the background and the surface, which a test
# holds it to, so nothing depends on a well-adjusted monitor.
DARK = Palette(
    dark=True,
    background="#1e1f22",
    surface="#2b2d30",
    border="#43454a",
    heading="#dfe1e5",
    text="#ced0d6",
    muted="#9da3ae",
    link="#589df6",
    hover="#323438",
    selection="#2e436e",
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
    border="#c9ccd6",
    heading="#111318",
    text="#27282e",
    muted="#575b66",
    link="#2e55a3",
    hover="#e6e8ec",
    selection="#d4e2ff",
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


def is_dark() -> bool:
    """Return whether the desktop is set to a dark theme, defaulting to dark when it cannot be told."""
    try:
        detected = darkdetect.isDark()
    except Exception as error:  # any failure here must not stop a window opening
        logger.debug("could not read the desktop theme: {}", error)
        return True
    if detected is None:
        logger.debug("this desktop does not say which theme it is set to, assuming dark")
        return True
    return bool(detected)


def palette(style: str = FOLLOW_DESKTOP) -> Palette:
    """Return the colours to draw with.

    :param style: ``dark`` or ``light`` to insist on one, or ``auto`` to follow whatever the desktop is set to
    """
    if style == ALWAYS_DARK:
        return DARK
    if style == ALWAYS_LIGHT:
        return LIGHT
    return DARK if is_dark() else LIGHT


def chosen_style() -> str:
    """Return the theme the settings ask for, read from the settings file directly.

    The settings module cannot be imported here, since it needs this one, and a window needs its colours before
    anything else. The file is small and read once as a window opens, so reading it twice costs nothing.
    """
    stored, _damaged = read_json(CONFIG_PATH)
    asked = stored.get("theme") if isinstance(stored, dict) else None
    return asked if asked in STYLES else FOLLOW_DESKTOP


PALETTE = palette(chosen_style())
