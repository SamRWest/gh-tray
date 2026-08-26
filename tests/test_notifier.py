"""Composing one notification out of several changes, and deciding which page a click opens."""

from __future__ import annotations

from gh_tray.notifier import MAX_LINES_PER_NOTIFICATION, Notifier


def event(kind: str, key: str = "acme/widget#7", url: str = "https://example.test/7") -> dict:
    """Build a minimal event of the given kind."""
    return {"kind": kind, "key": key, "url": url, "at": "2026-01-01T00:00:00Z", "title": "", "detail": ""}


def test_only_the_enabled_kinds_are_notified():
    chosen = Notifier().wanted([event("ci_broken"), event("new_comment")], {"ci_broken": True, "new_comment": False})
    assert [item["kind"] for item in chosen] == ["ci_broken"]


def test_nothing_is_notified_when_every_kind_is_switched_off():
    assert Notifier().wanted([event("ci_broken")], {"ci_broken": False}) == []


def test_one_change_is_described_in_the_singular():
    title, body = Notifier().compose([event("ci_broken")])
    assert title == "1 GitHub change"
    assert body == "Checks broke: acme/widget#7"


def test_several_changes_are_counted_in_the_title():
    title, _body = Notifier().compose([event("ci_broken"), event("mention")])
    assert title == "2 GitHub changes"


def test_a_long_list_is_collapsed_to_a_remainder_count():
    many = [event("ci_broken", key=f"acme/widget#{number}") for number in range(10)]
    title, body = Notifier().compose(many)
    assert title == "10 GitHub changes"
    assert body.count("\n") == MAX_LINES_PER_NOTIFICATION
    assert body.endswith(f"+{10 - MAX_LINES_PER_NOTIFICATION} more")


def test_notifying_nothing_raises_no_notification():
    assert Notifier().notify([event("ci_broken")], {"ci_broken": False}) is False


def test_a_click_opens_the_change_that_was_reported():
    assert Notifier().target_url([event("ci_broken", url="https://example.test/7")]) == "https://example.test/7"


def test_a_click_on_several_changes_opens_the_one_listed_first():
    # Never the dashboard: a click on a notification should always land on a pull request page.
    changes = [event("ci_broken", key="a#1", url="https://example.test/1"), event("mention", key="b#2", url="https://example.test/2")]
    assert Notifier().target_url(changes) == "https://example.test/1"


def test_a_change_without_a_page_is_skipped_in_favour_of_one_that_has_one():
    changes = [event("mention", key="a#1", url=""), event("ci_broken", key="b#2", url="https://example.test/2")]
    assert Notifier().target_url(changes) == "https://example.test/2"


def test_nothing_is_opened_when_no_change_carries_a_page():
    assert Notifier().target_url([event("mention", url=""), event("ci_broken", url="")]) == ""


def test_no_page_at_all_means_no_click_target_rather_than_an_error():
    assert Notifier().target_url([]) == ""
