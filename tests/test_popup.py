"""What the click-through window decides to show, and how it marks what is still unread.

The window itself is not built here: drawing it needs a display, and the parts worth protecting are the choice of
rows and their marking, both of which are ordinary functions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gh_tray import events, popup, snapshot


@pytest.fixture
def event_log(tmp_path, monkeypatch):
    """Point the event log, the seen marker and the snapshot at a temporary directory."""
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", tmp_path / "snapshot.json")
    return tmp_path


def change(kind: str = "ci_broken", key: str = "acme/widget#7", at: str | None = None) -> dict:
    """Build one recorded change."""
    repo, _, number = key.partition("#")
    return {
        "at": at or events.utc_now(),
        "kind": kind,
        "key": key,
        "repo": repo,
        "number": number,
        "url": f"https://example.test/{key}",
        "title": "",
        "detail": "",
    }


def waiting(number: int = 7, **overrides) -> dict:
    """Build one pull request as the last poll recorded it, awaiting the user's review unless overridden."""
    entry = {
        "side": "reviewing",
        "repo": "acme/gadget",
        "number": number,
        "title": "Please look at this",
        "url": f"https://example.test/gadget/{number}",
        "ci": "SUCCESS",
        "reviewDecision": "REVIEW_REQUIRED",
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "updatedAt": "2026-06-01T00:00:00Z",
        "author": "someone",
        "lastCommitBy": "",
        "lastReviewBy": "",
        "lastCommentBy": "",
    }
    entry.update(overrides)
    return entry


def store(tmp_path, *entries: dict) -> None:
    """Write pull requests into the snapshot the window reads."""
    snapshot.write_snapshot({f"{entry['side']}:{entry['repo']}#{entry['number']}": entry for entry in entries})


def test_the_newest_changes_come_first(event_log):
    for number in range(5):
        events.append_events([change(key=f"acme/widget#{number}")])
    assert [row.number for row in popup.rows_to_show(5)] == [f"#{number}" for number in reversed(range(5))]


def test_only_as_many_rows_as_asked_for_are_shown(event_log):
    events.append_events([change(key=f"acme/widget#{number}") for number in range(20)])
    assert len(popup.rows_to_show(3)) == 3


def test_asking_for_more_rows_than_exist_shows_what_there_is(event_log):
    events.append_events([change()])
    assert len(popup.rows_to_show(10)) == 1


def test_nothing_recorded_and_nothing_waiting_shows_nothing(event_log):
    assert popup.rows_to_show(8) == []


def test_changes_since_the_user_looked_are_marked_and_older_ones_are_not(event_log):
    events.append_events([change(key="acme/widget#1")])
    events.mark_seen()
    events.append_events([change(key="acme/widget#2")])
    marked = {row.number: row.colour for row in popup.rows_to_show(10)}
    assert marked["#2"] != popup.MUTED
    assert marked["#1"] == popup.MUTED


def test_everything_is_unread_before_the_user_has_ever_looked(event_log):
    events.append_events([change(), change(key="acme/widget#8")])
    assert all(row.colour != popup.MUTED for row in popup.rows_to_show(10))


def test_a_review_waiting_is_listed_even_when_nothing_has_changed(event_log):
    # The window used to say "nothing" while the hover text said three reviews were waiting.
    store(event_log, waiting())
    rows = popup.rows_to_show(10)
    assert [row.label for row in rows] == ["Awaiting your review"]
    assert rows[0].who == "someone"
    assert rows[0].colour == popup.URGENT


def test_what_changed_is_listed_before_what_is_merely_waiting(event_log):
    events.append_events([change(key="acme/widget#1")])
    store(event_log, waiting())
    assert [row.repo for row in popup.rows_to_show(10)] == ["acme/widget", "acme/gadget"]


def test_a_pull_request_is_not_listed_twice_when_it_both_changed_and_waits(event_log):
    entry = waiting()
    events.append_events([change(key="acme/gadget#7") | {"url": entry["url"]}])
    store(event_log, entry)
    assert len(popup.rows_to_show(10)) == 1


def test_the_states_worth_acting_on_are_recognised(event_log):
    store(
        event_log,
        waiting(1),
        waiting(2, side="authored", reviewDecision="CHANGES_REQUESTED", lastReviewBy="reviewer"),
        waiting(3, side="authored", ci="FAILURE", lastCommitBy="committer"),
        waiting(4, side="authored", reviewDecision="APPROVED", lastReviewBy="approver"),
    )
    assert {row.label for row in popup.rows_to_show(10)} == {
        "Awaiting your review",
        "Changes requested",
        "Checks failing",
        "Ready to merge",
    }


def test_a_pull_request_wanting_nothing_is_left_out(event_log):
    store(event_log, waiting(1, side="authored", reviewDecision="REVIEW_REQUIRED", ci="SUCCESS"))
    assert popup.rows_to_show(10) == []


def test_a_draft_is_not_offered_as_ready_to_merge(event_log):
    store(event_log, waiting(1, side="authored", reviewDecision="APPROVED", isDraft=True))
    assert popup.rows_to_show(10) == []


def test_blocking_items_are_listed_before_routine_ones(event_log):
    store(
        event_log,
        waiting(1, side="authored", reviewDecision="APPROVED", lastReviewBy="approver"),
        waiting(2, side="authored", ci="FAILURE", lastCommitBy="committer"),
    )
    assert [row.label for row in popup.rows_to_show(10)] == ["Checks failing", "Ready to merge"]


def test_the_most_recently_touched_comes_first_among_equals(event_log):
    store(
        event_log,
        waiting(1, updatedAt="2026-06-01T00:00:00Z"),
        waiting(2, updatedAt="2026-06-09T00:00:00Z"),
    )
    assert [row.number for row in popup.rows_to_show(10)] == ["#2", "#1"]


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
    assert all(isinstance(width, int) and width > 0 for _key, _heading, width, _stretches in popup.COLUMNS)
    assert [heading for _key, heading, _width, _stretches in popup.COLUMNS] == ["Change", "Repository", "PR", "Title", "Who", "When"]


def test_one_column_takes_the_space_a_resize_adds():
    stretching = [heading for _key, heading, _width, stretches in popup.COLUMNS if stretches]
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
