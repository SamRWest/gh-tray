"""Clicking a row in the window: left to open it, right to mark it seen.

Marking is a statement the user makes about one row, so it has a button of its own and nothing else changes it.
The handlers are exercised here against a stand-in table, with no display involved.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tksheet", reason="the window needs its table widget")

from gh_tray import events, popup, window


class Table:
    """A stand-in for the table widget, reporting whatever the test says was clicked."""

    def __init__(self) -> None:
        """Start with the first row of the table clicked."""
        self.region = "table"
        self.row: int | None = 0

    def identify_region(self, _event: object) -> str:
        """Return which part of the table the click landed on."""
        return self.region

    def identify_row(self, _event: object, allow_end: bool = True) -> int | None:
        """Return which row the click landed on."""
        return self.row


class Clicks:
    """A stand-in window carrying only what the click handlers touch."""

    on_click = window.Popup.on_click
    on_right_click = window.Popup.on_right_click
    clicked_row = window.Popup.clicked_row
    set_seen = window.Popup.set_seen
    heading_text = window.Popup.heading_text

    def __init__(self, entries: list[popup.Row]) -> None:
        """:param entries: the rows the table is showing."""
        self.entries = entries
        self.all_entries = list(entries)
        self.sheet = Table()
        self.opened: list[str] = []
        self.sorted_by: list[object] = []
        self.name = self
        self.redrawn = 0

    def configure(self, **_settings) -> None:
        """Stand in for the heading label, which is rewritten whenever a row is marked."""

    def refill(self) -> None:
        """Record that the table was redrawn."""
        self.redrawn += 1

    def open(self, url: str) -> None:
        """Record that a row was opened."""
        self.opened.append(url)

    def sort_from_heading(self, _event: object) -> None:
        """Record that a heading was clicked."""
        self.sorted_by.append(_event)


def row(number: str = "#7", seen: bool = False) -> popup.Row:
    """Build one row of the table."""
    return popup.Row(
        label="Checks broke",
        repo="acme/widget",
        number=number,
        title="Add a widget",
        who="someone",
        when="just now",
        url=f"https://example.test/{number.lstrip('#')}",
        colour=popup.URGENT,
        at="2026-01-01T00:00:00.000000Z",
        seen=seen,
    )


@pytest.fixture
def clicks(tmp_path, monkeypatch):
    """Build a stand-in window whose marks are written to a temporary directory."""
    monkeypatch.setattr(events, "SEEN_PATH", tmp_path / "seen.json")
    return Clicks([row("#7"), row("#8", seen=True)])


def test_a_right_click_marks_a_row_seen(clicks):
    clicks.on_right_click(None)
    assert clicks.entries[0].seen is True
    assert clicks.opened == []


def test_right_clicking_a_seen_row_marks_it_unseen(clicks):
    clicks.sheet.row = 1
    clicks.on_right_click(None)
    assert clicks.entries[1].seen is False


def test_a_mark_is_remembered_for_next_time(clicks):
    clicks.on_right_click(None)
    assert events.seen_marks()[clicks.entries[0].url]["seen"] is True


def test_a_left_click_opens_a_row_and_leaves_its_mark_alone(clicks):
    clicks.on_click(None)
    assert clicks.opened == [clicks.entries[0].url]
    assert clicks.entries[0].seen is False


def test_opening_a_seen_row_leaves_it_seen(clicks):
    clicks.sheet.row = 1
    clicks.on_click(None)
    assert clicks.entries[1].seen is True
    assert clicks.opened == [clicks.entries[1].url]


def test_a_left_click_on_a_heading_sorts(clicks):
    clicks.sheet.region = "header"
    clicks.on_click(None)
    assert len(clicks.sorted_by) == 1
    assert clicks.opened == []


def test_a_right_click_on_a_heading_marks_nothing(clicks):
    clicks.sheet.region = "header"
    clicks.on_right_click(None)
    assert [entry.seen for entry in clicks.entries] == [False, True]


def test_a_click_below_the_last_row_does_nothing(clicks):
    clicks.sheet.row = None
    clicks.on_click(None)
    clicks.on_right_click(None)
    assert [entry.seen for entry in clicks.entries] == [False, True]
    assert clicks.opened == []


def test_the_heading_counts_the_rows_not_yet_marked(clicks):
    assert clicks.heading_text().endswith("1 notification")
    clicks.on_right_click(None)
    assert clicks.heading_text().endswith("nothing to do")
