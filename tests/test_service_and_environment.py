"""One polling cycle end to end with a stubbed collector, and the platform helpers that can be checked safely."""

from __future__ import annotations

import json
import sys

import pytest

from gh_tray import config, environment, events, service, snapshot


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point every file the polling cycle touches at a temporary directory."""
    monkeypatch.setattr(snapshot, "SNAPSHOT_PATH", tmp_path / "snapshot.json")
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
    monkeypatch.setattr(service, "collect", lambda _config: (digest, error))


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


def test_an_unreadable_snapshot_rebuilds_the_baseline_without_reporting_everything_as_new(workspace, monkeypatch):
    (workspace / "snapshot.json").write_text("{ not json", encoding="utf-8")
    stub_collector(monkeypatch, digest_with())
    result = service.poll({})
    assert result.events == []
    # Not a first run: a damaged file is not proof the user has seen anything, so the unread count must survive.
    assert result.first_run is False
    assert json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))["version"] == snapshot.SNAPSHOT_VERSION


def test_a_still_unread_mention_is_only_reported_once(workspace, monkeypatch):
    mention = {"repo": "acme/widget", "title": "look", "url": "https://example.test/m1", "reason": "mention"}
    first = digest_with() | {"mentions": []}
    stub_collector(monkeypatch, first)
    service.poll({})
    stub_collector(monkeypatch, digest_with() | {"mentions": [mention]})
    assert [event["kind"] for event in service.poll({}).events] == ["mention"]
    assert service.poll({}).events == []


def test_the_login_start_file_lives_under_the_home_directory():
    assert environment.autostart_path().is_relative_to(environment.Path.home())


def test_the_login_start_file_names_this_application():
    assert "gh-tray" in environment.autostart_path().name


def test_the_launch_command_runs_this_package():
    assert environment.launch_command()[1:] == ["-m", "gh_tray"]


def test_switching_login_start_off_twice_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "autostart_path", lambda: tmp_path / "gh-tray.entry")
    environment.set_autostart(False)
    environment.set_autostart(False)


def test_a_console_is_only_hidden_on_windows():
    # Anywhere else the flag has to be nothing at all: a command refuses to start if asked for one it cannot give.
    assert (environment.no_console_flag() != 0) is (sys.platform == "win32")


def test_a_command_is_read_as_utf8_whatever_the_console_uses():
    # Written as bytes, which is how the GitHub tool writes: it says UTF-8 whatever codepage the console is on, and
    # reading that as the local one turns a tick into "a-hat" and mangles any title that is not plain English.
    written = "import sys; sys.stdout.buffer.write('✓ café'.encode())"
    assert environment.run_quietly([sys.executable, "-c", written]).stdout == "✓ café"


def test_a_command_that_fails_is_returned_rather_than_raised_on():
    assert environment.run_quietly([sys.executable, "-c", "raise SystemExit(3)"]).returncode == 3


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


def test_a_console_interrupt_runs_the_stop_it_was_given(monkeypatch):
    # The portable half: a signal handler is installed that runs the stop. The Windows console handler is the same
    # stop behind a platform call, proven by interrupting a real tray rather than from here.
    installed = {}
    monkeypatch.setattr(environment.sys, "platform", "linux")
    monkeypatch.setattr(environment.signal, "signal", lambda number, handler: installed.setdefault(number, handler))
    stopped = []
    environment.on_console_interrupt(lambda: stopped.append(True))
    installed[environment.signal.SIGINT](environment.signal.SIGINT, None)
    assert stopped == [True]


def test_the_pointer_is_located_or_honestly_not():
    # Windows, macOS and an X display can each say; anything else says nothing rather than guessing.
    spot = environment.cursor_position()
    assert spot is None or (len(spot) == 2 and all(isinstance(axis, int) for axis in spot))


def test_keeping_out_of_the_dock_is_harmless_everywhere():
    environment.hide_from_dock()
