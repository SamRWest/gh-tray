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
    # The two ends of the scale a date is drawn on, from something that just happened to something long forgotten.
    fresh: str
    stale: str


# Dark greys rather than blacks: a near-black window is a hole in the desktop, and the borders between its parts
# disappear into it.
DARK = Palette(
    dark=True,
    background="#22272e",
    surface="#2d333b",
    border="#444c56",
    heading="#f0f6fc",
    text="#e8eef4",
    muted="#adbac7",
    link="#6cb6ff",
    hover="#373e47",
    selection="#4184e4",
    red="#ff7b72",
    orange="#ffa657",
    amber="#f2cc60",
    green="#5ddb6f",
    blue="#79c0ff",
    violet="#d2a8ff",
    pink="#ff9bce",
    fresh="#a5d6ff",
    stale="#7c8fbf",
)

# Darker inks throughout: the same hues at dark-mode brightness are unreadable on white.
LIGHT = Palette(
    dark=False,
    background="#ffffff",
    surface="#f2f5f8",
    border="#ccd5de",
    heading="#111a24",
    text="#1f2937",
    muted="#4f5b67",
    link="#0969da",
    hover="#e8eef4",
    selection="#ddf4ff",
    red="#cf222e",
    orange="#bc4c00",
    amber="#8a6400",
    green="#1a7f37",
    blue="#0969da",
    violet="#8250df",
    pink="#bf3989",
    fresh="#0550ae",
    stale="#6e83a8",
)


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


def palette() -> Palette:
    """Return the colours to draw with, following the desktop's theme."""
    return DARK if is_dark() else LIGHT


PALETTE = palette()
