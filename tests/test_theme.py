"""Following the desktop's light or dark theme, and staying readable either way."""

from __future__ import annotations

import pytest

from gh_tray import theme
from gh_tray.popup import blend


def test_a_dark_desktop_gets_the_dark_colours(monkeypatch):
    monkeypatch.setattr(theme.darkdetect, "isDark", lambda: True)
    assert theme.palette() is theme.DARK


def test_a_light_desktop_gets_the_light_colours(monkeypatch):
    monkeypatch.setattr(theme.darkdetect, "isDark", lambda: False)
    assert theme.palette() is theme.LIGHT


def test_a_desktop_that_does_not_say_is_treated_as_dark(monkeypatch):
    # Several Linux desktops report nothing at all, and a dark window on a light desktop is the lesser surprise.
    monkeypatch.setattr(theme.darkdetect, "isDark", lambda: None)
    assert theme.palette() is theme.DARK


def test_a_desktop_that_cannot_be_asked_does_not_stop_a_window_opening(monkeypatch):
    def refuse():
        raise OSError("no such setting")

    monkeypatch.setattr(theme.darkdetect, "isDark", refuse)
    assert theme.palette() is theme.DARK


def test_both_palettes_name_every_colour():
    assert set(vars(theme.DARK)) == set(vars(theme.LIGHT))


def test_no_colour_is_left_unset():
    for palette in (theme.DARK, theme.LIGHT):
        for name, value in vars(palette).items():
            if name == "dark":
                continue
            assert isinstance(value, str) and value.startswith("#") and len(value) == 7, f"{name} is not a colour"


def brightness(colour: str) -> float:
    """Return how light a colour is, weighted the way an eye sees each channel.

    :param colour: a colour written as six hexadecimal digits after a hash
    """
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    return (0.299 * red + 0.587 * green + 0.114 * blue) / 255


@pytest.mark.parametrize("name", ["text", "heading", "muted", "urgent", "routine", "good", "link"])
def test_every_ink_stands_out_from_its_background(name):
    # A colour picked for a dark window is unreadable on a white one, which is why there are two sets and not one.
    for palette in (theme.DARK, theme.LIGHT):
        gap = abs(brightness(getattr(palette, name)) - brightness(palette.background))
        assert gap > 0.2, f"{name} is too close to the background in the {'dark' if palette.dark else 'light'} palette"


def test_the_two_palettes_are_the_opposite_way_round():
    assert brightness(theme.DARK.background) < brightness(theme.LIGHT.background)
    assert brightness(theme.DARK.text) > brightness(theme.LIGHT.text)


@pytest.mark.parametrize("name", ["urgent", "routine", "good"])
def test_a_faded_row_still_stands_out_from_its_background(name):
    # The oldest rows are drawn at the weakest strength, and must not fade into the background entirely.
    from gh_tray.popup import AGE_FADE

    weakest = min(weight for _limit, weight in AGE_FADE)
    for palette in (theme.DARK, theme.LIGHT):
        faded = blend(getattr(palette, name), palette.background, weakest)
        assert abs(brightness(faded) - brightness(palette.background)) > 0.05, f"{name} disappears when old"
