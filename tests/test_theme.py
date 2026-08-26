"""Following the desktop's light or dark theme, and staying readable either way."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from PIL import ImageColor

from gh_tray import theme
from gh_tray.theme import blend


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


@pytest.mark.parametrize("name", ["text", "heading", "muted", "link", "red", "orange", "amber", "green", "blue", "violet", "pink"])
def test_every_ink_stands_out_from_its_background(name):
    # A colour picked for a dark window is unreadable on a white one, which is why there are two sets and not one.
    for palette in (theme.DARK, theme.LIGHT):
        gap = abs(brightness(getattr(palette, name)) - brightness(palette.background))
        assert gap > 0.2, f"{name} is too close to the background in the {'dark' if palette.dark else 'light'} palette"


def test_the_two_palettes_are_the_opposite_way_round():
    assert brightness(theme.DARK.background) < brightness(theme.LIGHT.background)
    assert brightness(theme.DARK.text) > brightness(theme.LIGHT.text)


@pytest.mark.parametrize("name", ["red", "orange", "amber", "green", "blue", "violet", "pink"])
def test_a_faded_row_still_stands_out_from_its_background(name):
    # The oldest rows are drawn at the weakest strength, and must not fade into the background entirely.

    from gh_tray.popup import SEEN_STRENGTH

    weakest = SEEN_STRENGTH
    for palette in (theme.DARK, theme.LIGHT):
        faded = blend(getattr(palette, name), palette.background, weakest)
        assert abs(brightness(faded) - brightness(palette.background)) > 0.05, f"{name} disappears when old"


def test_the_theme_can_be_forced_either_way():
    assert theme.palette(theme.ALWAYS_DARK) is theme.DARK
    assert theme.palette(theme.ALWAYS_LIGHT) is theme.LIGHT


def test_following_the_desktop_is_what_auto_means(monkeypatch):
    monkeypatch.setattr(theme.darkdetect, "isDark", lambda: False)
    assert theme.palette(theme.FOLLOW_DESKTOP) is theme.LIGHT
    monkeypatch.setattr(theme.darkdetect, "isDark", lambda: True)
    assert theme.palette(theme.FOLLOW_DESKTOP) is theme.DARK


def test_an_unknown_theme_setting_falls_back_to_following_the_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(theme, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"theme": "chartreuse"}', encoding="utf-8")
    assert theme.chosen_style() == theme.FOLLOW_DESKTOP


def test_the_chosen_theme_is_read_from_the_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(theme, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"theme": "light"}', encoding="utf-8")
    assert theme.chosen_style() == theme.ALWAYS_LIGHT


def test_no_settings_yet_means_following_the_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(theme, "CONFIG_PATH", tmp_path / "absent.json")
    assert theme.chosen_style() == theme.FOLLOW_DESKTOP


def test_the_application_mark_is_drawn_at_every_size_windows_asks_for(tmp_path):
    from gh_tray.status import APP_ICON_SIZES, app_icon, write_app_icon

    for size in APP_ICON_SIZES:
        assert app_icon(size).size == (size, size)
    written = write_app_icon(tmp_path / "icon.ico")
    assert written.exists() and written.stat().st_size > 0


def test_the_mark_carries_its_three_colours_at_every_size_it_is_asked_for():
    # The mark is three coloured dots on a dark field. At sixteen pixels the dots are two pixels across, so what
    # matters is that each colour still reaches the picture rather than being smoothed away into the field.
    from gh_tray.status import APP_ICON_SIZES, ICON_ROWS, app_icon

    for size in APP_ICON_SIZES:
        drawn = app_icon(size).convert("RGB")
        for _middle, _width, colour in ICON_ROWS:
            wanted = ImageColor.getrgb(colour)
            assert any(close_to(pixel, wanted) for pixel in channels(drawn)), f"{colour} is missing at {size} pixels"


def channels(image) -> list[tuple]:
    """Return every pixel of a colour picture as its channels.

    A picture holding one number per pixel has no channels to compare, so those are left out rather than guessed at.
    """
    return [pixel for pixel in image.get_flattened_data() if isinstance(pixel, tuple)]


def close_to(pixel: Sequence[int], wanted: Sequence[int], allowance: int = 60) -> bool:
    """Return whether a drawn pixel is recognisably one of the mark's colours, after smoothing has moved it."""
    return all(abs(drawn - asked) <= allowance for drawn, asked in zip(pixel, wanted, strict=True))
