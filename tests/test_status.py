"""How a poll result becomes a colour, a count and hover text."""

from __future__ import annotations

from gh_tray import status
from gh_tray.status import AMBER, GREEN, GREY, RED, Status


def digest(authored=(), reviewing=()) -> dict:
    """Build a collector result from lists of check states."""
    return {
        "authored": [{"ci": state} for state in authored],
        "reviewing": [{"ci": state} for state in reviewing],
    }


def event(kind: str) -> dict:
    """Build a minimal event of the given kind."""
    return {"kind": kind, "at": "2026-01-01T00:00:00Z", "key": "a#1"}


def test_nothing_unread_is_green():
    assert status.status_from(digest(authored=["SUCCESS"]), []).colour == GREEN


def test_an_unread_blocking_change_is_red():
    assert status.status_from(digest(authored=["SUCCESS"]), [event("review_requested")]).colour == RED


def test_an_unread_non_blocking_change_is_amber():
    assert status.status_from(digest(authored=["SUCCESS"]), [event("ready_to_merge")]).colour == AMBER


def test_one_blocking_change_among_several_makes_it_red():
    unread = [event("new_comment"), event("ci_broken"), event("ready_to_merge")]
    assert status.status_from(digest(authored=["SUCCESS"]), unread).colour == RED


def test_a_failed_poll_is_grey():
    result = status.status_from({}, [], error="collector timed out")
    assert result.colour == GREY
    assert result.error == "collector timed out"


def test_failing_and_pending_checks_are_counted_across_both_lists():
    result = status.status_from(digest(authored=["FAILURE", "SUCCESS", "ERROR"], reviewing=["PENDING", "FAILURE"]), [])
    assert result.authored == 3
    assert result.reviewing == 2
    assert result.red == 3
    assert result.pending == 1


def test_a_backlog_of_red_pull_requests_does_not_by_itself_raise_the_colour():
    assert status.status_from(digest(authored=["FAILURE"] * 20), []).colour == GREEN


def test_hover_text_fits_the_platform_limit():
    result = Status(authored=999, reviewing=999, red=999, pending=999, polled_at="23:59", unread=999, colour=RED)
    assert len(status.tooltip_text(result)) <= status.TOOLTIP_LIMIT


def test_hover_text_for_a_long_error_fits_the_platform_limit():
    result = Status(colour=GREY, error="x" * 300)
    assert len(status.tooltip_text(result)) <= status.TOOLTIP_LIMIT


def test_hover_text_names_the_app_and_the_unread_count():
    text = status.tooltip_text(Status(unread=3, polled_at="09:00"), app_name="gh-tray")
    assert text.startswith("gh-tray - 3 unread changes")


def test_hover_text_says_when_there_is_nothing_to_look_at():
    assert "no changes" in status.tooltip_text(Status(unread=0, polled_at="09:00"))


def test_one_unread_change_reads_in_the_singular():
    assert "1 unread change\n" in status.tooltip_text(Status(unread=1, polled_at="09:00"))


def test_the_menu_header_reports_a_failed_poll():
    assert status.summary_line(Status(colour=GREY, error="bash not found")).startswith("Poll failed")


def test_the_menu_header_reports_the_three_counts():
    assert status.summary_line(Status(authored=25, reviewing=4, red=15)) == "4 to review - 15 red - 25 open"


def centre_pixel(colour: str, count: int) -> tuple:
    """Return the colour at the middle of the drawn icon."""
    return status.build_image(colour, count).getpixel((status.ICON_SIZE // 2, status.ICON_SIZE // 2))


def test_the_icon_is_filled_in_the_colour_it_is_given():
    for colour, expected in ((RED, (209, 36, 47)), (GREEN, (26, 127, 55)), (GREY, (110, 119, 129))):
        assert centre_pixel(colour, 0)[:3] == expected


def test_a_count_is_drawn_over_the_disc():
    # The count is drawn in white through the middle, so the centre pixel must stop being the fill colour.
    assert centre_pixel(RED, 3) != centre_pixel(RED, 0)


def test_a_large_count_is_still_drawn():
    # Drawn as "9+", which straddles the middle, so the whole image is compared rather than one pixel.
    assert status.build_image(RED, 400).tobytes() != status.build_image(RED, 0).tobytes()


def test_the_icon_is_drawn_for_every_state():
    for colour in (RED, AMBER, GREEN, GREY):
        for count in (0, 1, 9, 10, 400):
            image = status.build_image(colour, count)
            assert image.size == (status.ICON_SIZE, status.ICON_SIZE)
            assert image.getpixel((0, 0))[3] == 0, "the icon's corners must be transparent, not boxed"
