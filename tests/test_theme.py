"""Following the desktop's light or dark theme, and staying readable either way."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from PIL import Image, ImageColor
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from gh_tray import theme
from gh_tray.theme import blend

# The offscreen platform used for testing has no real desktop behind it, so QStyleHints.setColorScheme() is a no-op
# and colorScheme() always reads back Unknown; is_dark() is exercised by replacing what colorScheme() itself
# returns, which needs a QApplication to exist first (an implicit dependency every test below carries via qapp).


def forced_scheme(qapp, monkeypatch, scheme: Qt.ColorScheme) -> None:
    """Make the toolkit report a chosen colour scheme for the rest of the test.

    :param qapp: ensures a QApplication exists before the platform theme hint is touched
    :param monkeypatch: used to replace the hint, restored automatically at the end of the test
    :param scheme: the scheme ``is_dark`` should read back
    """
    monkeypatch.setattr(QGuiApplication.styleHints(), "colorScheme", lambda: scheme)


def test_a_dark_desktop_gets_the_dark_colours(qapp, monkeypatch):
    forced_scheme(qapp, monkeypatch, Qt.ColorScheme.Dark)
    assert theme.palette() is theme.DARK


def test_a_light_desktop_gets_the_light_colours(qapp, monkeypatch):
    forced_scheme(qapp, monkeypatch, Qt.ColorScheme.Light)
    assert theme.palette() is theme.LIGHT


def test_a_desktop_that_does_not_say_is_treated_as_dark(qapp, monkeypatch):
    # Several Linux desktops report nothing at all, and a dark window on a light desktop is the lesser surprise.
    forced_scheme(qapp, monkeypatch, Qt.ColorScheme.Unknown)
    assert theme.palette() is theme.DARK


def test_following_the_desktop_returns_a_palette_under_the_offscreen_platform(qapp):
    assert isinstance(theme.palette(theme.FOLLOW_DESKTOP), theme.Palette)


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


@pytest.mark.parametrize("name", ["muted", "red", "orange", "amber", "green", "blue", "violet", "pink"])
def test_every_ink_stands_out_from_its_background(name):
    # A colour picked for a dark window is unreadable on a white one, which is why there are two sets and not one.
    for palette in (theme.DARK, theme.LIGHT):
        gap = abs(brightness(getattr(palette, name)) - brightness(palette.background))
        assert gap > 0.2, f"{name} is too close to the background in the {'dark' if palette.dark else 'light'} palette"


def test_the_two_palettes_are_the_opposite_way_round():
    assert brightness(theme.DARK.background) < brightness(theme.LIGHT.background)
    assert brightness(theme.DARK.muted) > brightness(theme.LIGHT.muted)


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


def test_following_the_desktop_is_what_auto_means(qapp, monkeypatch):
    forced_scheme(qapp, monkeypatch, Qt.ColorScheme.Light)
    assert theme.palette(theme.FOLLOW_DESKTOP) is theme.LIGHT
    forced_scheme(qapp, monkeypatch, Qt.ColorScheme.Dark)
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


def test_the_application_mark_is_drawn_at_every_size_a_desktop_shows_it():
    from gh_tray.status import APP_ICON_SIZES, app_icon

    for size in APP_ICON_SIZES:
        assert app_icon(size).size == (size, size)


def test_the_application_mark_is_written_as_a_portable_picture(tmp_path):
    # A Windows icon file is refused by the toolkit everywhere else, so the file has to be a picture every desktop
    # reads.
    from gh_tray.status import APP_ICON_SIZE, write_app_icon

    written = write_app_icon(tmp_path / "mark.png")
    with Image.open(written) as picture:
        assert picture.format == "PNG"
        assert picture.size == (APP_ICON_SIZE, APP_ICON_SIZE)


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


def wcag_contrast(ink: str, ground: str) -> float:
    """Return the WCAG contrast ratio between an ink and what it is drawn on."""

    def luminance(colour: str) -> float:
        def channel(value: int) -> float:
            share = value / 255
            return share / 12.92 if share <= 0.04045 else ((share + 0.055) / 1.055) ** 2.4

        red, green, blue = (channel(int(colour[index : index + 2], 16)) for index in (1, 3, 5))
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    brighter, darker = sorted((luminance(ink), luminance(ground)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(
    "name", ["muted", "red", "orange", "amber", "green", "blue", "violet", "pink", "fresh", "stale"]
)
def test_every_ink_reads_at_wcag_aa_on_both_grounds(name):
    # 4.5 to 1 is the WCAG AA floor for ordinary text, held against both surfaces an ink can be drawn on, in both
    # palettes, so readability does not depend on a well-adjusted monitor or a forgiving theme choice.
    for palette in (theme.DARK, theme.LIGHT):
        for ground in (palette.background, palette.surface):
            found = wcag_contrast(getattr(palette, name), ground)
            assert found >= 4.5, (
                f"{name} reads at {found:.2f}:1 on {ground} in the {'dark' if palette.dark else 'light'} palette"
            )
