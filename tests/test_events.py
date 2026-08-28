"""Change detection: every rule fires on its transition, and nothing fires without one."""

from __future__ import annotations

import json

import pytest

from gh_tray import events


def pull_request(**overrides) -> dict:
    """Build a snapshot record, green and unremarkable unless overridden."""
    record = {
        "side": "authored",
        "repo": "acme/widget",
        "number": 7,
        "title": "Add a widget",
        "url": "https://github.com/acme/widget/pull/7",
        "ci": "SUCCESS",
        "reviewDecision": "NONE",
        "mergeable": "MERGEABLE",
        "comments": 0,
        "isDraft": False,
    }
    record.update(overrides)
    return record


def detect(before: dict | None, after: dict, login: str = "") -> list[str]:
    """Return the kinds of event produced by moving one pull request from one state to another."""
    previous = {} if before is None else {"acme/widget#7": before}
    found = events.detect_pull_request_events(previous, {"acme/widget#7": after}, "2026-01-01T00:00:00Z", login)
    return [event["kind"] for event in found]


def test_no_change_produces_no_events():
    assert detect(pull_request(), pull_request()) == []


def test_a_pull_request_already_failing_is_not_reported_again():
    assert detect(pull_request(ci="FAILURE"), pull_request(ci="FAILURE")) == []


def test_checks_breaking_is_reported():
    assert detect(pull_request(ci="SUCCESS"), pull_request(ci="FAILURE")) == ["ci_broken"]


def test_checks_erroring_is_reported():
    assert detect(pull_request(ci="PENDING"), pull_request(ci="ERROR")) == ["ci_broken"]


def test_checks_recovering_is_not_reported():
    assert detect(pull_request(ci="FAILURE"), pull_request(ci="SUCCESS")) == []


def test_changes_requested_is_reported_once():
    assert detect(pull_request(), pull_request(reviewDecision="CHANGES_REQUESTED")) == ["changes_requested"]
    assert detect(pull_request(reviewDecision="CHANGES_REQUESTED"), pull_request(reviewDecision="CHANGES_REQUESTED")) == []


def test_becoming_ready_to_merge_is_reported():
    assert detect(pull_request(), pull_request(reviewDecision="APPROVED")) == ["ready_to_merge"]


def test_an_approved_draft_is_not_ready_to_merge():
    assert detect(pull_request(isDraft=True), pull_request(reviewDecision="APPROVED", isDraft=True)) == []


def test_an_approved_failing_pull_request_is_not_ready_to_merge():
    assert detect(pull_request(ci="FAILURE"), pull_request(reviewDecision="APPROVED", ci="FAILURE")) == []


def test_a_new_conflict_is_reported():
    assert detect(pull_request(), pull_request(mergeable="CONFLICTING")) == ["conflict"]


def test_a_conflict_clearing_is_not_reported():
    assert detect(pull_request(mergeable="CONFLICTING"), pull_request(mergeable="MERGEABLE")) == []


def test_new_comments_are_reported_with_a_count():
    previous = {"acme/widget#7": pull_request(comments=2)}
    found = events.detect_pull_request_events(previous, {"acme/widget#7": pull_request(comments=5)}, "2026-01-01T00:00:00Z")
    assert [event["kind"] for event in found] == ["new_comment"]
    assert found[0]["detail"] == "3 new"


def test_a_falling_comment_count_is_not_reported():
    assert detect(pull_request(comments=5), pull_request(comments=2)) == []


def test_a_pull_request_new_to_the_review_queue_is_reported():
    assert detect(None, pull_request(side="reviewing")) == ["review_requested"]


def test_a_pull_request_new_to_the_authored_list_is_not_reported():
    assert detect(None, pull_request(side="authored")) == []


def test_several_changes_at_once_are_all_reported():
    kinds = detect(pull_request(), pull_request(ci="FAILURE", mergeable="CONFLICTING", comments=1))
    assert sorted(kinds) == ["ci_broken", "conflict", "new_comment"]


def test_snapshot_keeps_the_fields_the_rules_read():
    digest = {
        "authored": [{"key": "acme/widget#7", **pull_request()}],
        "reviewing": [{"key": "acme/gadget#1", **pull_request(repo="acme/gadget", number=1)}],
    }
    snapshot = events.snapshot_of(digest)
    assert set(snapshot) == {"authored:acme/widget#7", "reviewing:acme/gadget#1"}
    assert snapshot["authored:acme/widget#7"]["side"] == "authored"
    assert snapshot["reviewing:acme/gadget#1"]["side"] == "reviewing"
    for field in events.SNAPSHOT_FIELDS:
        assert field in snapshot["authored:acme/widget#7"]


def test_a_pull_request_without_checks_is_recorded_as_having_none():
    digest = {"authored": [{"key": "acme/widget#7", "repo": "acme/widget", "number": 7}]}
    assert events.snapshot_of(digest)["authored:acme/widget#7"]["ci"] == "NO_CHECKS"


def test_a_mention_already_recorded_is_not_reported_again():
    digest = {"mentions": [{"repo": "acme/widget", "title": "look at this", "url": "https://example.test/1", "reason": "team_mention"}]}
    assert events.detect_mention_events(digest, set(), "2026-01-01T00:00:00Z")[0]["kind"] == "mention"
    assert events.detect_mention_events(digest, {"https://example.test/1"}, "2026-01-01T00:00:00Z") == []


def test_a_mention_reason_is_described_in_words():
    digest = {"mentions": [{"repo": "acme/widget", "url": "https://example.test/1", "reason": "team_mention"}]}
    assert events.detect_mention_events(digest, set(), "2026-01-01T00:00:00Z")[0]["detail"] == "team mention"


@pytest.fixture
def event_log(tmp_path, monkeypatch):
    """Point the event log and the seen marker at a temporary directory."""
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "SEEN_PATH", tmp_path / "seen.json")
    return tmp_path


def test_unread_counts_only_what_arrived_since_the_user_looked(event_log):
    events.append_events([{"at": "2026-01-01T00:00:00Z", "kind": "ci_broken", "key": "a#1", "url": "", "title": "", "detail": ""}])
    events.mark_seen()
    assert events.unread_events() == []
    events.append_events([{"at": "2099-01-01T00:00:00Z", "kind": "mention", "key": "a#2", "url": "", "title": "", "detail": ""}])
    assert [event["key"] for event in events.unread_events()] == ["a#2"]


def test_marking_seen_keeps_the_history(event_log):
    events.append_events([{"at": "2026-01-01T00:00:00Z", "kind": "ci_broken", "key": "a#1", "url": "", "title": "", "detail": ""}])
    events.mark_seen()
    assert events.unread_events() == []
    assert len(events.read_events()) == 1


def test_an_unreadable_line_in_the_log_is_skipped(event_log):
    (event_log / "events.jsonl").write_text('{"at":"2026-01-01T00:00:00Z","kind":"mention","key":"a#1"}\nnot json\n', encoding="utf-8")
    assert len(events.read_events()) == 1


def test_events_are_returned_newest_first(event_log):
    for stamp in ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"):
        events.append_events([{"at": stamp, "kind": "mention", "key": stamp, "url": "", "title": "", "detail": ""}])
    assert [event["key"] for event in events.recent_events()] == ["2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"]


def test_mention_addresses_are_collected_from_history():
    history = [
        {"kind": "mention", "url": "https://example.test/1"},
        {"kind": "ci_broken", "url": "https://example.test/2"},
        {"kind": "mention", "url": ""},
    ]
    assert events.mention_urls(history) == {"https://example.test/1"}


def test_every_rule_a_detector_can_emit_has_wording():
    # The wording table and the detectors must not drift apart, or a change would be reported under a bare key.
    emitted = {
        "review_requested",
        "ci_broken",
        "changes_requested",
        "ready_to_merge",
        "conflict",
        "new_comment",
        "mention",
    }
    assert emitted <= set(events.RULE_LABELS)
    assert all(events.label_for(kind) != kind for kind in emitted)


def test_blocking_changes_are_marked_urgent_and_others_are_not():
    assert [events.is_urgent(kind) for kind in ("review_requested", "ci_broken", "changes_requested", "mention")] == [True] * 4
    assert [events.is_urgent(kind) for kind in ("ready_to_merge", "conflict", "new_comment")] == [False] * 3


def test_an_unknown_kind_falls_back_to_its_own_name():
    assert events.label_for("something_new") == "something_new"
    assert events.is_urgent("something_new") is False


def test_appending_nothing_leaves_no_file(event_log):
    events.append_events([])
    assert not (event_log / "events.jsonl").exists()


def test_events_are_written_one_json_document_per_line(event_log):
    events.append_events([{"at": "2026-01-01T00:00:00Z", "kind": "mention", "key": "a#1", "url": "", "title": "", "detail": ""}] * 2)
    lines = (event_log / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["kind"] == "mention" for line in lines)


def test_a_comment_the_user_wrote_themselves_is_not_reported_to_them():
    # Their own comment is not news to them, and it was cluttering the list with rows they had just caused.
    previous = {"authored:acme/widget#7": pull_request(comments=1, lastCommentBy="them")}
    current = {"authored:acme/widget#7": pull_request(comments=2, lastCommentBy="them")}
    digest = {"viewer": "them", "mentions": []}
    assert events.detect_events(previous, current, digest) == []


def test_a_comment_somebody_else_wrote_is_still_reported():
    previous = {"authored:acme/widget#7": pull_request(comments=1, lastCommentBy="them")}
    current = {"authored:acme/widget#7": pull_request(comments=2, lastCommentBy="somebody")}
    digest = {"viewer": "them", "mentions": []}
    assert [event["kind"] for event in events.detect_events(previous, current, digest)] == ["new_comment"]


def test_the_users_own_commit_breaking_the_checks_is_still_reported():
    # They caused it, but they still need to know: doing it and knowing about it are not the same act.
    previous = {"authored:acme/widget#7": pull_request(ci="SUCCESS", lastCommitBy="them")}
    current = {"authored:acme/widget#7": pull_request(ci="FAILURE", lastCommitBy="them")}
    digest = {"viewer": "them", "mentions": []}
    assert [event["kind"] for event in events.detect_events(previous, current, digest)] == ["ci_broken"]


def test_nothing_is_suppressed_when_who_is_signed_in_is_unknown():
    previous = {"authored:acme/widget#7": pull_request(comments=1, lastCommentBy="them")}
    current = {"authored:acme/widget#7": pull_request(comments=2, lastCommentBy="them")}
    assert [event["kind"] for event in events.detect_events(previous, current, {"mentions": []})] == ["new_comment"]


def test_a_mention_the_user_wrote_themselves_is_not_reported():
    digest = {"viewer": "them", "mentions": [{"repo": "acme/widget", "url": "https://example.test/1", "actor": "them"}]}
    assert events.detect_events({}, {}, digest) == []


def test_a_row_marked_seen_by_hand_leaves_the_unread_count(event_log):
    events.append_events(
        [{"at": "2099-01-01T00:00:00Z", "kind": "ci_broken", "key": "a#1", "url": "https://example.test/1", "title": "", "detail": ""}]
    )
    assert len(events.unread_events()) == 1
    events.remember_seen("https://example.test/1", "2099-01-01T00:00:00Z", seen=True)
    assert events.unread_events() == []


def test_a_row_marked_unseen_by_hand_outlasts_marking_everything_seen(event_log):
    events.append_events(
        [{"at": "2026-01-01T00:00:00Z", "kind": "ci_broken", "key": "a#1", "url": "https://example.test/1", "title": "", "detail": ""}]
    )
    events.remember_seen("https://example.test/1", "2026-01-01T00:00:00Z", seen=False)
    events.mark_seen()
    assert events.unread_events() == []


def test_marking_everything_seen_forgets_the_rows_marked_one_at_a_time(event_log):
    events.remember_seen("https://example.test/1", "2026-01-01T00:00:00Z", seen=True)
    events.mark_seen()
    assert events.seen_marks() == {}


def test_a_change_after_a_mark_undoes_it(event_log):
    events.remember_seen("https://example.test/1", "2026-01-01T00:00:00Z", seen=True)
    marks = events.seen_marks()
    assert events.has_been_seen("https://example.test/1", "2026-01-01T00:00:00Z", marks, None) is True
    assert events.has_been_seen("https://example.test/1", "2026-02-01T00:00:00Z", marks, None) is False


def test_a_row_is_remembered_by_its_address_and_by_its_number_where_there_is_none():
    assert events.row_identity("https://example.test/1", "acme/widget", "7") == "https://example.test/1"
    assert events.row_identity("", "acme/widget", "7") == "acme/widget#7"
    assert events.row_identity("", "acme/widget", "#7") == "acme/widget#7"


def test_only_the_newest_marks_are_kept():
    marks = {f"row-{number}": {"at": "", "seen": True, "marked": f"2026-01-{number + 1:02d}T00:00:00Z"} for number in range(9)}
    kept = events.newest_marks(marks, keep=3)
    assert set(kept) == {"row-8", "row-7", "row-6"}
    assert events.newest_marks(marks, keep=20) == marks


def test_a_conflict_on_someone_else_s_pull_request_is_left_to_them():
    # A conflict is for whoever has to rebase. On a pull request you are only reviewing, that is not you.
    reviewing = {"side": "reviewing"}
    assert detect(pull_request(**reviewing), pull_request(mergeable="CONFLICTING", **reviewing)) == []


def test_a_comment_on_your_own_pull_request_is_reported():
    assert detect(pull_request(comments=1), pull_request(comments=2), login="me") == ["new_comment"]


def test_a_comment_on_a_pull_request_you_merely_review_is_left_to_its_author():
    reviewing = {"side": "reviewing"}
    assert detect(pull_request(comments=1, **reviewing), pull_request(comments=2, **reviewing), login="me") == []


def test_an_answer_to_one_of_your_review_comments_is_reported_wherever_it_is():
    reviewing = {"side": "reviewing"}
    answered = pull_request(comments=2, lastCommentAnswers="me", **reviewing)
    assert detect(pull_request(comments=1, **reviewing), answered, login="me") == ["new_comment"]


def test_an_answer_to_somebody_else_is_not_yours_to_hear_about():
    reviewing = {"side": "reviewing"}
    answered = pull_request(comments=2, lastCommentAnswers="them", **reviewing)
    assert detect(pull_request(comments=1, **reviewing), answered, login="me") == []
