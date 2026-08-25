"""One polling cycle end to end with a stubbed collector, and the platform helpers that can be checked safely."""

from __future__ import annotations

import json
import sys

import pytest

from gh_tray import config, environment, events, service


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point every file the polling cycle touches at a temporary directory."""
    monkeypatch.setattr(service, "SNAPSHOT_PATH", tmp_path / "snapshot.json")
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "SEEN_PATH", tmp_path / "seen.json")
    return tmp_path


def digest_with(ci: str = "SUCCESS", comments: int = 0) -> dict:
    """Build a collector result holding one authored pull request."""
    return {
        "authored": [
            {
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
        ],
        "reviewing": [],
        "mentions": [],
    }


def stub_collector(monkeypatch, digest: dict | None, error: str = ""):
    """Make the polling cycle read a fixed digest instead of running the collector."""
    monkeypatch.setattr(service, "run_digest", lambda _config: (digest, error))


def test_the_first_poll_establishes_a_baseline_without_inventing_changes(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with(ci="FAILURE"))
    result = service.poll({})
    assert result.first_run is True
    assert result.events == []
    assert result.status.red == 1


def test_the_second_poll_reports_what_moved(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with(ci="SUCCESS"))
    service.poll({})
    stub_collector(monkeypatch, digest_with(ci="FAILURE"))
    result = service.poll({})
    assert [event["kind"] for event in result.events] == ["ci_broken"]
    assert result.first_run is False


def test_an_unchanged_second_poll_reports_nothing(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with())
    service.poll({})
    result = service.poll({})
    assert result.events == []
    assert result.status.unread == 0


def test_changes_stay_unread_until_the_user_looks(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with(comments=0))
    service.poll({})
    stub_collector(monkeypatch, digest_with(comments=1))
    assert service.poll({}).status.unread == 1
    # Polling again must not quietly clear what the user has not read.
    assert service.poll({}).status.unread == 1
    events.mark_seen()
    assert service.poll({}).status.unread == 0


def test_a_failed_poll_reports_the_error_and_writes_no_baseline(workspace, monkeypatch):
    stub_collector(monkeypatch, None, error="collector timed out")
    result = service.poll({})
    assert result.error == "collector timed out"
    assert not (workspace / "snapshot.json").exists()


def test_a_failed_poll_keeps_earlier_unread_changes(workspace, monkeypatch):
    stub_collector(monkeypatch, digest_with(comments=0))
    service.poll({})
    stub_collector(monkeypatch, digest_with(comments=1))
    service.poll({})
    stub_collector(monkeypatch, None, error="collector timed out")
    assert service.poll({}).status.unread == 1


def test_an_unreadable_snapshot_is_treated_as_a_fresh_baseline(workspace, monkeypatch):
    (workspace / "snapshot.json").write_text("{ not json", encoding="utf-8")
    stub_collector(monkeypatch, digest_with())
    result = service.poll({})
    assert result.first_run is True
    assert json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))


def test_a_still_unread_mention_is_only_reported_once(workspace, monkeypatch):
    mention = {"repo": "acme/widget", "title": "look", "url": "https://example.test/m1", "reason": "mention"}
    first = digest_with() | {"mentions": []}
    stub_collector(monkeypatch, first)
    service.poll({})
    stub_collector(monkeypatch, digest_with() | {"mentions": [mention]})
    assert [event["kind"] for event in service.poll({}).events] == ["mention"]
    assert service.poll({}).events == []


def test_bash_is_taken_from_the_settings_when_one_is_named():
    assert environment.find_bash("/somewhere/bash") == "/somewhere/bash"


def test_bash_is_discovered_when_the_settings_name_none():
    # Every platform this runs on has either bash or git, so discovery should return something.
    assert environment.find_bash("") is not None


def test_the_login_start_file_lives_under_the_home_directory():
    assert environment.autostart_path().is_relative_to(environment.Path.home())


def test_the_login_start_file_names_this_application():
    assert "gh-tray" in environment.autostart_path().name


def test_the_launch_command_runs_this_package():
    assert environment.launch_command()[1:] == ["-m", "gh_tray"]


def test_login_start_can_be_switched_on_and_off(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "autostart_path", lambda: tmp_path / "nested" / "gh-tray.entry")
    assert environment.autostart_enabled() is False
    environment.set_autostart(True)
    assert environment.autostart_enabled() is True
    assert environment.launch_command()[1] in (tmp_path / "nested" / "gh-tray.entry").read_text(encoding="utf-8")
    environment.set_autostart(False)
    assert environment.autostart_enabled() is False


def test_switching_login_start_off_twice_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "autostart_path", lambda: tmp_path / "gh-tray.entry")
    environment.set_autostart(False)
    environment.set_autostart(False)


def test_hidden_window_flags_are_only_used_on_windows():
    flags = environment.hidden_window_flags()
    assert ("creationflags" in flags) is (sys.platform == "win32")


def test_only_one_instance_can_hold_the_lock(tmp_path):
    first = environment.SingleInstance(tmp_path / "app.lock")
    second = environment.SingleInstance(tmp_path / "app.lock")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_releasing_a_lock_never_taken_is_harmless(tmp_path):
    environment.SingleInstance(tmp_path / "app.lock").release()


def test_the_application_directory_is_named_after_the_application():
    assert config.APP_DIR.name == "gh-tray"
