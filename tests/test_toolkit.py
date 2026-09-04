"""Following the theme setting: the scheme the windows are drawn in, and the widget style and palette that draw it."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt, qInstallMessageHandler, qWarning

from gh_tray import toolkit


@pytest.fixture(autouse=True)
def back_to_following(qapp):
    yield
    toolkit.follow_theme_setting("auto")


def test_toolkit_messages_land_in_the_log_at_their_level(qapp):
    records: list[tuple[str, str]] = []
    handle = toolkit.logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])), level="DEBUG"
    )
    toolkit.route_toolkit_messages()
    try:
        qWarning("something regrettable")
    finally:
        qInstallMessageHandler(None)
        toolkit.logger.remove(handle)
    assert ("WARNING", "toolkit: something regrettable") in records


def test_insisting_on_dark_or_light_is_what_is_wanted(qapp):
    assert toolkit.wanted_scheme("dark") == Qt.ColorScheme.Dark
    assert toolkit.wanted_scheme("light") == Qt.ColorScheme.Light


def test_a_desktop_that_cannot_be_told_is_drawn_dark_to_match_the_inks(qapp):
    # The offscreen platform, like a server edition of Windows, has no theme setting to read.
    assert toolkit.wanted_scheme("auto") == Qt.ColorScheme.Dark
    toolkit.follow_theme_setting("auto")
    assert qapp.palette().window().color().lightness() < 128


def test_a_platform_that_refuses_a_scheme_is_handed_a_palette_and_given_its_own_back(qapp):
    # The offscreen platform ignores a requested scheme, so the palette is what makes the windows dark or light.
    toolkit.follow_theme_setting("dark")
    assert qapp.palette().window().color().lightness() < 128
    toolkit.follow_theme_setting("light")
    assert qapp.palette().window().color().lightness() > 128


def test_a_style_that_stays_light_gives_way_to_one_that_follows_the_scheme(qapp, monkeypatch):
    # Pretend the application started with the older Windows style, which draws light whatever it is told.
    monkeypatch.setattr(
        qapp,
        "property",
        lambda name: toolkit.STYLE_THAT_STAYS_LIGHT if name == toolkit.STARTING_STYLE_PROPERTY else None,
    )
    toolkit.follow_theme_setting("dark")
    assert qapp.style().name().casefold() == toolkit.SCHEME_FOLLOWING_STYLE.casefold()
