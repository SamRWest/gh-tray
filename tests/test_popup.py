"""What the click-through window decides to show, and how it marks what is still unread.

The window itself is not built here: drawing it needs a display, and the parts worth protecting are the choice of
rows and their marking, both of which are ordinary functions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gh_tray import events, popup, snapshot, theme


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
    marked = {row.number: row.seen for row in popup.rows_to_show(10)}
    assert marked["#2"] is False
    assert marked["#1"] is True


def test_everything_is_unread_before_the_user_has_ever_looked(event_log):
    events.append_events([change(), change(key="acme/widget#8")])
    assert all(not row.seen for row in popup.rows_to_show(10))


def test_a_review_waiting_is_listed_even_when_nothing_has_changed(event_log):
    # The window used to say "nothing" while the hover text said three reviews were waiting.
    store(event_log, waiting())
    rows = popup.rows_to_show(10)
    assert [row.label for row in rows] == ["Awaiting your review"]
    assert rows[0].who == "someone"
    assert rows[0].colour == popup.PALETTE.orange


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


def test_each_sort_of_change_has_its_own_colour():
    assert popup.dot_colour(change("ci_broken"), unread=True) == popup.URGENT
    assert popup.dot_colour(change("new_comment")) == popup.PALETTE.blue
    assert popup.dot_colour(change("mention")) == popup.PALETTE.violet
    assert len({popup.dot_colour(change(kind)) for kind in popup.KIND_COLOURS}) == len(popup.KIND_COLOURS)


def test_a_change_already_seen_keeps_saying_what_sort_of_thing_it_was():
    # Turning a seen row grey would say it had been switched off rather than merely read; it is dimmed instead.
    assert popup.dot_colour(change("ci_broken"), unread=False) == popup.URGENT


def test_no_row_is_ever_drawn_in_plain_grey():
    from gh_tray import theme

    for palette in (theme.DARK, theme.LIGHT):
        for name in ("red", "orange", "amber", "green", "blue", "violet", "pink", "fresh", "stale"):
            red, green, blue = (int(getattr(palette, name)[index : index + 2], 16) for index in (1, 3, 5))
            assert max(red, green, blue) - min(red, green, blue) > 12, f"{name} is a grey"


def test_a_row_is_marked_filled_until_seen_and_hollow_after():
    assert popup.glyph_for(popup.Row("", "", "", "", "", "", "", popup.URGENT)) == popup.UNSEEN_GLYPH

    assert popup.glyph_for(popup.Row("", "", "", "", "", "", "", popup.URGENT, seen=True)) == popup.SEEN_GLYPH


def test_a_date_is_drawn_further_along_its_scale_the_older_it_is():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    fresh = popup.age_colour((now - timedelta(minutes=5)).strftime(events.TIMESTAMP_FORMAT), now=now)
    middling = popup.age_colour((now - timedelta(days=14)).strftime(events.TIMESTAMP_FORMAT), now=now)
    stale = popup.age_colour((now - timedelta(days=400)).strftime(events.TIMESTAMP_FORMAT), now=now)
    assert fresh == popup.PALETTE.fresh
    assert stale == popup.PALETTE.stale
    assert middling not in (fresh, stale), "the scale should pass through the colours between its two ends"


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


def test_a_conflict_names_the_author_whose_branch_takes_the_rebase():
    # GitHub does not record whose merge caused a conflict, so the one name that means something is the author's.
    record = {"repo": "acme/widget", "number": 7, "author": "someone"}
    assert events._event("conflict", record, "", events.utc_now())["actor"] == "someone"


def test_a_mention_names_whoever_wrote_it():
    digest = {"mentions": [{"repo": "acme/widget", "url": "https://example.test/1", "actor": "someone", "reason": "mention"}]}
    assert events.detect_mention_events(digest, set(), events.utc_now())[0]["actor"] == "someone"


def test_a_mention_nobody_could_be_found_for_still_appears():
    digest = {"mentions": [{"repo": "acme/widget", "url": "https://example.test/1", "reason": "mention"}]}
    assert events.detect_mention_events(digest, set(), events.utc_now())[0]["actor"] == ""


def test_something_ready_to_merge_is_good_news_rather_than_a_warning(event_log):
    store(event_log, waiting(1, side="authored", reviewDecision="APPROVED", lastReviewBy="approver"))
    assert popup.rows_to_show(10)[0].colour == popup.GOOD


def test_a_ready_to_merge_change_is_green_too():
    assert popup.dot_colour(change("ready_to_merge"), unread=True) == popup.GOOD


def test_the_newest_row_is_at_the_top_whatever_it_came_from(event_log):
    old = waiting(1, updatedAt="2020-01-01T00:00:00Z")
    store(event_log, old)
    events.append_events([change(key="acme/widget#1")])
    assert popup.rows_to_show(10)[0].repo == "acme/widget"


def test_a_change_older_than_what_is_waiting_sinks_below_it(event_log):
    store(event_log, waiting(1, updatedAt=events.utc_now().replace(".", "")[:19] + "Z"))
    events.append_events([change(key="acme/widget#1", at="2020-01-01T00:00:00.000000Z")])
    assert popup.rows_to_show(10)[0].repo == "acme/gadget"


def test_a_column_of_numbers_sorts_by_size_not_by_spelling():
    rows = [
        popup.Row("", "acme/widget", "#7", "", "", "", "", popup.URGENT),
        popup.Row("", "acme/widget", "#128", "", "", "", "", popup.URGENT),
    ]
    assert [row.number for row in popup.sorted_rows(rows, "pr", newest_first=False)] == ["#7", "#128"]


def test_a_row_with_no_number_sorts_without_failing():
    rows = [popup.Row("", "acme/widget", "", "", "", "", "", popup.URGENT)]
    assert popup.sorted_rows(rows, "pr", newest_first=False) == rows


def test_text_columns_sort_regardless_of_case():
    rows = [
        popup.Row("beta", "", "", "", "", "", "", popup.URGENT),
        popup.Row("Alpha", "", "", "", "", "", "", popup.URGENT),
    ]
    assert [row.label for row in popup.sorted_rows(rows, "change", newest_first=False)] == ["Alpha", "beta"]


def test_rows_with_nobody_named_sort_last():
    rows = [
        popup.Row("", "", "", "", "", "", "", popup.URGENT),
        popup.Row("", "", "", "", "someone", "", "", popup.URGENT),
    ]
    assert [row.who for row in popup.sorted_rows(rows, "who", newest_first=False)] == ["someone", ""]


def test_an_unknown_column_falls_back_to_the_usual_order():
    rows = [
        popup.Row("", "", "", "", "", "", "", popup.URGENT, at="2020-01-01T00:00:00.000000Z"),
        popup.Row("", "", "", "", "", "", "", popup.URGENT, at="2026-01-01T00:00:00.000000Z"),
    ]
    assert popup.sorted_rows(rows, "invented")[0].at.startswith("2026")


def test_a_row_dims_only_once_it_has_been_seen():
    # Age used to dim a row as well, leaving two rows of the same sort looking different for no nameable reason.
    assert 0.0 < popup.SEEN_STRENGTH < 1.0
    assert not hasattr(popup, "fade_for"), "age no longer dims a row; it has a scale of its own in the date column"


def test_fading_keeps_the_hue_and_only_dims_it():
    faded = theme.blend(popup.URGENT, popup.BACKGROUND, 0.5)
    assert faded != popup.URGENT
    assert theme.blend(popup.URGENT, popup.BACKGROUND, 1.0) == popup.URGENT
    assert theme.blend(popup.URGENT, popup.BACKGROUND, 0.0) == popup.BACKGROUND


def test_several_comments_on_one_pull_request_are_one_row(event_log):
    # The list is what wants attention, not a history: three comments on one pull request are one thing to look at.
    for _ in range(3):
        events.append_events([change("new_comment", key="acme/widget#7")])
    assert len(popup.rows_to_show(10)) == 1


def test_the_most_recent_of_several_rows_for_one_pull_request_is_the_one_kept(event_log):
    events.append_events([change("new_comment", key="acme/widget#7", at="2020-01-01T00:00:00.000000Z")])
    events.append_events([change("ci_broken", key="acme/widget#7", at="2026-01-01T00:00:00.000000Z")])
    assert [row.label for row in popup.rows_to_show(10)] == ["Checks broke"]


def test_different_pull_requests_are_not_folded_together(event_log):
    events.append_events([change("new_comment", key="acme/widget#7"), change("new_comment", key="acme/widget#8")])
    assert len(popup.rows_to_show(10)) == 2


def test_rows_with_no_address_are_told_apart_by_repository_and_number():
    rows = [
        popup.Row("", "acme/widget", "#7", "", "", "", "", popup.URGENT),
        popup.Row("", "acme/widget", "#8", "", "", "", "", popup.URGENT),
        popup.Row("", "acme/widget", "#7", "", "", "", "", popup.URGENT),
    ]
    assert len(popup.one_per_pull_request(rows)) == 2


def test_clicking_a_row_marks_it_seen(event_log):
    events.append_events([change(key="acme/widget#1")])
    popup.remember_row_seen(popup.rows_to_show(10)[0], True)
    assert popup.rows_to_show(10)[0].seen is True


def test_clicking_a_seen_row_again_marks_it_unseen(event_log):
    events.append_events([change(key="acme/widget#1")])
    events.mark_seen()
    popup.remember_row_seen(popup.rows_to_show(10)[0], False)
    assert popup.rows_to_show(10)[0].seen is False


def test_a_row_marked_seen_comes_back_when_something_happens_to_it(event_log):
    # Marking says "I have read this", not "stop telling me about this pull request".
    events.append_events([change(key="acme/widget#1", at="2026-01-01T00:00:00.000000Z")])
    popup.remember_row_seen(popup.rows_to_show(10)[0], True)
    events.append_events([change(kind="new_comment", key="acme/widget#1", at="2026-02-01T00:00:00.000000Z")])
    assert popup.rows_to_show(10)[0].seen is False


def test_a_review_waiting_is_not_dimmed_by_marking_everything_seen(event_log):
    # A review is still waiting however long ago the user last cleared the list, so it stays at full strength.
    store(event_log, waiting())
    events.mark_seen()
    assert popup.rows_to_show(10)[0].seen is False


def test_a_review_waiting_can_still_be_marked_seen_by_clicking_it(event_log):
    store(event_log, waiting())
    popup.remember_row_seen(popup.rows_to_show(10)[0], True)
    assert popup.rows_to_show(10)[0].seen is True


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """Point the remembered window shape at a temporary directory."""
    monkeypatch.setattr(popup, "LAYOUT_PATH", tmp_path / "layout.json")
    return tmp_path


def test_nothing_is_remembered_until_the_user_drags_something(layout):
    assert popup.remembered_width(96) is None
    assert popup.remembered_column_widths(96) == {}


def test_a_dragged_width_comes_back_as_it_was_on_the_same_display(layout):
    popup.remember_width(800, 96)
    assert popup.remembered_width(96) == 800


def test_a_dragged_width_scales_with_the_display_it_comes_back_on(layout):
    # Remembered at standard scaling and played back on a display drawing twice as finely, the window should take
    # the same share of the screen, which means twice the pixels.
    popup.remember_width(800, 96)
    assert popup.remembered_width(192) == 1600


def test_column_widths_are_remembered_by_name(layout):
    popup.remember_column_widths({"repo": 300, "title": 400}, 96)
    assert popup.remembered_column_widths(96) == {"repo": 300, "title": 400}
    assert popup.remembered_column_widths(48) == {"repo": 150, "title": 200}


def test_remembering_columns_keeps_the_window_width_and_the_other_way_round(layout):
    popup.remember_width(800, 96)
    popup.remember_column_widths({"repo": 300}, 96)
    assert popup.remembered_width(96) == 800
    popup.remember_width(900, 96)
    assert popup.remembered_column_widths(96) == {"repo": 300}


def test_a_hand_damaged_layout_reads_as_nothing_remembered(layout):
    (layout / "layout.json").write_text('{"window": {"width": "wide", "dots": 0}, "columns": []}', encoding="utf-8")
    assert popup.remembered_width(96) is None
    assert popup.remembered_column_widths(96) == {}


def test_the_same_name_is_always_dealt_the_same_colour():
    assert popup.who_colour("SamRWest") == popup.who_colour("SamRWest")
    assert popup.who_colour("SamRWest") in popup.NAME_COLOURS


def test_names_spread_across_the_colours_rather_than_sharing_one():
    # The dealing is a digest, so a small sample lands unevenly; what matters is that names do spread, and that a
    # large enough sample reaches every colour there is.
    dealt = {popup.who_colour(f"reviewer-{number}") for number in range(200)}
    assert dealt == set(popup.NAME_COLOURS)


def test_a_missing_name_gets_the_quiet_ink():
    assert popup.who_colour("") == popup.PALETTE.muted
