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
    """Every colour a window draws with."""

    dark: bool
    background: str
    surface: str
    border: str
    heading: str
    text: str
    muted: str
    link: str
    urgent: str
    routine: str
    good: str
    hover: str
    selection: str


DARK = Palette(
    dark=True,
    background="#0d1117",
    surface="#161b22",
    border="#30363d",
    heading="#e6edf3",
    text="#f0f6fc",
    muted="#9198a1",
    link="#79c0ff",
    urgent="#ff7b72",
    routine="#e3b341",
    good="#3fb950",
    hover="#21262d",
    selection="#1f6feb",
)

# Darker inks throughout: the same hues at dark-mode brightness are unreadable on white.
LIGHT = Palette(
    dark=False,
    background="#ffffff",
    surface="#f6f8fa",
    border="#d0d7de",
    heading="#1f2328",
    text="#1f2328",
    muted="#636c76",
    link="#0969da",
    urgent="#cf222e",
    routine="#9a6700",
    good="#1a7f37",
    hover="#eaeef2",
    selection="#ddf4ff",
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
