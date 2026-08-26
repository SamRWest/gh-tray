"""What the click-through window decides to show, and how it marks what is still unread.

The window itself is not built here: drawing it needs a display, and the parts worth protecting are the choice of
rows and their marking, both of which are ordinary functions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gh_tray import events, popup


@pytest.fixture
def event_log(tmp_path, monkeypatch):
    """Point the event log and the seen marker at a temporary directory."""
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "SEEN_PATH", tmp_path / "seen.json")
    return tmp_path


def change(kind: str = "ci_broken", key: str = "acme/widget#7", at: str | None = None) -> dict:
    """Build one recorded change."""
    return {"at": at or events.utc_now(), "kind": kind, "key": key, "url": f"https://example.test/{key}", "title": "", "detail": ""}


def test_the_newest_changes_come_first(event_log):
    for number in range(5):
        events.append_events([change(key=f"acme/widget#{number}")])
    shown = popup.rows_to_show(5)
    assert [entry["key"] for entry, _unread in shown] == [f"acme/widget#{number}" for number in reversed(range(5))]


def test_only_as_many_rows_as_asked_for_are_shown(event_log):
    events.append_events([change(key=f"acme/widget#{number}") for number in range(20)])
    assert len(popup.rows_to_show(3)) == 3


def test_asking_for_more_rows_than_exist_shows_what_there_is(event_log):
    events.append_events([change()])
    assert len(popup.rows_to_show(10)) == 1


def test_an_empty_log_shows_nothing_rather_than_failing(event_log):
    assert popup.rows_to_show(8) == []


def test_changes_since_the_user_looked_are_marked_unread(event_log):
    events.append_events([change(key="acme/widget#1")])
    events.mark_seen()
    events.append_events([change(key="acme/widget#2")])
    shown = dict((entry["key"], unread) for entry, unread in popup.rows_to_show(10))
    assert shown["acme/widget#2"] is True
    assert shown["acme/widget#1"] is False


def test_everything_is_unread_before_the_user_has_ever_looked(event_log):
    events.append_events([change(), change(key="acme/widget#8")])
    assert all(unread for _entry, unread in popup.rows_to_show(10))


def test_a_blocking_change_is_marked_more_loudly_than_a_routine_one():
    assert popup.dot_colour(change("ci_broken"), unread=True) == popup.URGENT
    assert popup.dot_colour(change("new_comment"), unread=True) == popup.ROUTINE


def test_a_change_already_seen_is_marked_quietly_whatever_it_was():
    assert popup.dot_colour(change("ci_broken"), unread=False) == popup.MUTED


def test_ages_are_described_in_the_shortest_accurate_form():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    cases = {
        timedelta(seconds=5): "just now",
        timedelta(minutes=1): "just now",
        timedelta(minutes=5): "5m ago",
        timedelta(hours=3): "3h ago",
        timedelta(days=2): "2d ago",
        timedelta(days=20): "2w ago",
    }
    for ago, expected in cases.items():
        stamp = (now - ago).strftime(events.TIMESTAMP_FORMAT)
        assert events.age_in_words(stamp, now=now) == expected


def test_a_change_stamped_in_the_future_is_not_described_as_negative():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    stamp = (now + timedelta(hours=1)).strftime(events.TIMESTAMP_FORMAT)
    assert events.age_in_words(stamp, now=now) == "just now"


def test_an_unreadable_timestamp_still_produces_words():
    assert events.age_in_words("whenever").endswith("ago")


def test_a_repository_and_number_are_offered_as_separate_values():
    assert popup.repo_and_number({"repo": "acme/widget", "number": 7}) == ("acme/widget", "#7")


def test_an_older_entry_with_only_the_joined_key_is_split():
    # Entries logged before the two were recorded separately must still fill both columns.
    assert popup.repo_and_number({"key": "acme/widget#7"}) == ("acme/widget", "#7")


def test_a_mention_has_a_repository_but_no_number():
    assert popup.repo_and_number({"repo": "acme/widget", "number": ""}) == ("acme/widget", "")


def test_every_column_has_a_heading_and_a_width():
    assert all(isinstance(width, int) and width >= 0 for _heading, width in popup.COLUMNS)
    assert [heading for heading, _width in popup.COLUMNS if heading] == ["Change", "Repository", "PR", "Title", "Who", "When"]


def test_one_column_takes_the_space_a_resize_adds():
    stretching = [heading for heading, width in popup.COLUMNS if not width and heading]
    assert stretching == [popup.STRETCHING_COLUMN]


def test_the_person_behind_each_kind_of_change_is_named():
    for kind, field in events.ACTOR_FIELDS.items():
        record = {"repo": "acme/widget", "number": 7, field: "someone"}
        assert events._event(kind, record, "", events.utc_now())["actor"] == "someone"


def test_a_conflict_names_nobody_because_github_attributes_it_to_nobody():
    record = {"repo": "acme/widget", "number": 7, "author": "someone"}
    assert events._event("conflict", record, "", events.utc_now())["actor"] == ""


def test_a_mention_names_whoever_wrote_it():
    digest = {"mentions": [{"repo": "acme/widget", "url": "https://example.test/1", "actor": "someone", "reason": "mention"}]}
    assert events.detect_mention_events(digest, set(), events.utc_now())[0]["actor"] == "someone"


def test_a_mention_nobody_could_be_found_for_still_appears():
    digest = {"mentions": [{"repo": "acme/widget", "url": "https://example.test/1", "reason": "mention"}]}
    assert events.detect_mention_events(digest, set(), events.utc_now())[0]["actor"] == ""
