"""Damaged state, awkward settings and concurrent polls must not lose or duplicate a change.

These cases cover the ways the application can quietly do the wrong thing: silently marking unread changes as seen,
reporting the same change twice, crashing on a hand-edited file, or re-announcing something after a transient
collector failure.
"""

from __future__ import annotations

import json
import threading

import pytest

from gh_tray import config, events, service


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point every file the polling cycle touches at a temporary directory."""
    monkeypatch.setattr(service, "SNAPSHOT_PATH", tmp_path / "snapshot.json")
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "SEEN_PATH", tmp_path / "seen.json")
    return tmp_path


def digest_with(ci: str = "SUCCESS", comments: int = 0, reviewing: bool = False) -> dict:
    """Build a collector result holding one pull request on the chosen side."""
    entry = {
        "key": "acme/widget#7",
        "repo": "acme/widget",
        "number": 7,
        "title": "Add a widget",
        "url": "https://example.test/7",
        "ci": ci,
        "reviewDecision": "NONE",
        "mergeable": "MERGEABLE",
        "comments": comments,
        "isDraft": False,
    }
    side = "reviewing" if reviewing else "authored"
    return {"authored": [], "reviewing": [], side: [entry], "mentions": []}


def stub_collector(monkeypatch, digest: dict | None, error: str = ""):
    """Make the polling cycle read a fixed digest instead of running the collector."""
    monkeypatch.setattr(service, "collect", lambda _config: (digest, error))


def test_a_damaged_snapshot_does_not_mark_unread_changes_as_seen(workspace, monkeypatch):
    # A truncated state file must not be mistaken for a fresh start, which would silently clear the unread count.
    stub_collector(monkeypatch, digest_with(comments=0))
    service.poll({})
    stub_collector(monkeypatch, digest_with(comments=1))
    assert service.poll({}).status.unread == 1
    (workspace / "snapshot.json").write_text('{"version": 2, "entries": {"authored:acm', encoding="utf-8")
    result = service.poll({})
    assert result.first_run is False
    assert result.status.unread == 1


def test_a_snapshot_from_an_older_version_is_replaced_without_reporting_everything_as_new(workspace, monkeypatch):
    (workspace / "snapshot.json").write_text(json.dumps({"version": 1, "entries": {}}), encoding="utf-8")
    stub_collector(monkeypatch, digest_with(reviewing=True))
    assert service.poll({}).events == []


def test_the_snapshot_is_written_whole_or_not_at_all(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with())
    service.poll({})
    stored = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
    assert stored["version"] == service.SNAPSHOT_VERSION
    assert not list(workspace.glob("*.tmp"))


def test_a_pull_request_missing_from_one_poll_is_not_new_when_it_returns(workspace, monkeypatch):
    # The collector's GitHub queries fail intermittently, so a gap must not read as a departure and an arrival.
    stub_collector(monkeypatch, digest_with(reviewing=True))
    service.poll({})
    stub_collector(monkeypatch, {"authored": [], "reviewing": [], "mentions": []})
    assert service.poll({}).events == []
    stub_collector(monkeypatch, digest_with(reviewing=True))
    assert service.poll({}).events == []


def test_a_pull_request_gone_for_long_enough_counts_as_new_when_it_returns(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with(reviewing=True))
    service.poll({})
    stub_collector(monkeypatch, {"authored": [], "reviewing": [], "mentions": []})
    for _ in range(events.ABSENCE_GRACE_POLLS + 1):
        service.poll({})
    stub_collector(monkeypatch, digest_with(reviewing=True))
    assert [event["kind"] for event in service.poll({}).events] == ["review_requested"]


def test_the_same_pull_request_on_both_sides_keeps_both_histories():
    entry = {
        "key": "acme/widget#7",
        "repo": "acme/widget",
        "number": 7,
        "ci": "SUCCESS",
        "reviewDecision": "NONE",
        "mergeable": "MERGEABLE",
        "comments": 0,
        "isDraft": False,
    }
    snapshot = events.snapshot_of({"authored": [entry], "reviewing": [entry]})
    assert set(snapshot) == {"authored:acme/widget#7", "reviewing:acme/widget#7"}


def test_concurrent_polls_record_each_change_exactly_once(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with(comments=0))
    service.poll({})
    stub_collector(monkeypatch, digest_with(comments=1))

    lock = threading.Lock()

    def guarded_poll() -> None:
        """Poll the way the tray does, holding the same lock it holds."""
        with lock:
            service.poll({})

    threads = [threading.Thread(target=guarded_poll) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [event["kind"] for event in events.read_events()] == ["new_comment"]


def test_a_seen_marker_in_an_older_timestamp_format_still_hides_older_events(workspace):
    # Text comparison would sort a microsecond stamp before a whole-second one, so moments are compared instead.
    events.append_events([{"at": "2026-01-01T00:00:00.500000Z", "kind": "mention", "key": "a#1", "url": "", "title": "", "detail": ""}])
    (workspace / "seen.json").write_text(json.dumps({"lastSeenAt": "2026-01-01T00:00:01Z"}), encoding="utf-8")
    assert events.unread_events() == []


def test_a_seen_marker_in_an_older_timestamp_format_still_shows_newer_events(workspace):
    events.append_events([{"at": "2026-01-01T00:00:02.000000Z", "kind": "mention", "key": "a#1", "url": "", "title": "", "detail": ""}])
    (workspace / "seen.json").write_text(json.dumps({"lastSeenAt": "2026-01-01T00:00:01Z"}), encoding="utf-8")
    assert len(events.unread_events()) == 1


def test_an_unreadable_timestamp_is_treated_as_old_rather_than_crashing(workspace):
    events.append_events([{"at": "whenever", "kind": "mention", "key": "a#1", "url": "", "title": "", "detail": ""}])
    events.mark_seen()
    assert events.unread_events() == []


def test_marking_seen_shortens_the_log(workspace):
    events.append_events([{"at": events.utc_now(), "kind": "mention", "key": f"a#{n}", "url": "", "title": "", "detail": ""} for n in range(400)])
    events.mark_seen()
    assert len(events.read_events()) == events.EVENT_TAIL_KEPT


def test_the_log_cannot_grow_without_limit_even_if_nobody_looks(workspace):
    batch = [{"at": events.utc_now(), "kind": "mention", "key": f"a#{n}", "url": "", "title": "", "detail": ""} for n in range(900)]
    for _ in range(3):
        events.append_events(batch)
    assert len(events.read_events()) <= events.EVENT_HARD_LIMIT


def test_the_unread_count_covers_more_than_one_page_of_history(workspace):
    events.mark_seen()
    events.append_events([{"at": events.utc_now(), "kind": "mention", "key": f"a#{n}", "url": "", "title": "", "detail": ""} for n in range(600)])
    assert len(events.unread_events()) == 600


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point the settings file at a temporary directory."""
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    return tmp_path / "config.json"


@pytest.mark.parametrize(
    "contents",
    ['{"toasts": null}', '{"toasts": "yes please"}', '{"toasts": ["ci_broken"]}', "[1, 2, 3]", '"just a string"', "null"],
    ids=["null-toasts", "string-toasts", "list-toasts", "top-level-list", "top-level-string", "top-level-null"],
)
def test_a_settings_file_of_the_wrong_shape_still_loads(settings_file, contents):
    # The settings window is the way to repair settings, so a settings error must never stop it opening.
    settings_file.write_text(contents, encoding="utf-8")
    loaded = config.load_config()
    assert loaded["toasts"]["ci_broken"] is True
    assert loaded["poll_minutes"] == config.DEFAULT_CONFIG["poll_minutes"]


def test_settings_are_written_whole_or_not_at_all(settings_file):
    config.save_config(config.load_config())
    assert json.loads(settings_file.read_text(encoding="utf-8"))["poll_minutes"]
    assert not list(settings_file.parent.glob("*.tmp"))


def test_a_conflict_is_not_reported_again_when_github_forgets_and_remembers(workspace, monkeypatch):
    # GitHub works out mergeability only when asked, so a pull request commonly reads as unknown for one poll.
    def conflicting(mergeable: str) -> dict:
        digest = digest_with()
        digest["authored"][0]["mergeable"] = mergeable
        return digest

    stub_collector(monkeypatch, conflicting("MERGEABLE"))
    service.poll({})
    stub_collector(monkeypatch, conflicting("CONFLICTING"))
    assert [event["kind"] for event in service.poll({}).events] == ["conflict"]
    stub_collector(monkeypatch, conflicting("UNKNOWN"))
    assert service.poll({}).events == []
    stub_collector(monkeypatch, conflicting("CONFLICTING"))
    assert service.poll({}).events == []


def test_a_conflict_clearing_through_unknown_is_still_noticed_when_it_returns(workspace, monkeypatch):
    def with_mergeable(mergeable: str) -> dict:
        digest = digest_with()
        digest["authored"][0]["mergeable"] = mergeable
        return digest

    stub_collector(monkeypatch, with_mergeable("MERGEABLE"))
    service.poll({})
    stub_collector(monkeypatch, with_mergeable("UNKNOWN"))
    service.poll({})
    stub_collector(monkeypatch, with_mergeable("CONFLICTING"))
    assert [event["kind"] for event in service.poll({}).events] == ["conflict"]


def test_an_unknown_value_is_not_stored_as_though_it_were_real(workspace, monkeypatch):
    def with_mergeable(mergeable: str) -> dict:
        digest = digest_with()
        digest["authored"][0]["mergeable"] = mergeable
        return digest

    stub_collector(monkeypatch, with_mergeable("CONFLICTING"))
    service.poll({})
    stub_collector(monkeypatch, with_mergeable("UNKNOWN"))
    service.poll({})
    stored = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
    assert stored["entries"]["authored:acme/widget#7"]["mergeable"] == "CONFLICTING"
