"""Settings loading, defaulting and repair."""

from __future__ import annotations

import json

import pytest

from gh_tray import config


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point the settings file and the application directory at a temporary directory."""
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    return tmp_path / "config.json"


def test_defaults_are_used_when_nothing_is_stored(settings_file):
    loaded = config.load_config()
    assert loaded["poll_minutes"] == config.DEFAULT_CONFIG["poll_minutes"]
    assert loaded["dashboard_command"] == ""


def test_stored_values_win_over_defaults(settings_file):
    settings_file.write_text(json.dumps({"poll_minutes": 3, "dashboard_command": "my-dash"}), encoding="utf-8")
    loaded = config.load_config()
    assert loaded["poll_minutes"] == 3
    assert loaded["dashboard_command"] == "my-dash"


def test_a_missing_notification_switch_falls_back_to_its_default(settings_file):
    settings_file.write_text(json.dumps({"toasts": {"mention": False}}), encoding="utf-8")
    loaded = config.load_config()
    assert loaded["toasts"]["mention"] is False
    assert loaded["toasts"]["ci_broken"] is True


def test_an_unknown_notification_switch_is_dropped(settings_file):
    settings_file.write_text(json.dumps({"toasts": {"invented": True}}), encoding="utf-8")
    assert "invented" not in config.load_config()["toasts"]


def test_a_corrupt_settings_file_falls_back_to_defaults(settings_file):
    settings_file.write_text("{ not json", encoding="utf-8")
    assert config.load_config()["poll_minutes"] == config.DEFAULT_CONFIG["poll_minutes"]


def test_a_poll_interval_below_the_minimum_is_raised(settings_file):
    settings_file.write_text(json.dumps({"poll_minutes": 0}), encoding="utf-8")
    assert config.load_config()["poll_minutes"] == config.NUMBER_RANGES["poll_minutes"][0]


def test_a_popup_taller_than_the_screen_is_capped(settings_file):
    settings_file.write_text(json.dumps({"popup_rows": 5000}), encoding="utf-8")
    assert config.load_config()["popup_rows"] == config.NUMBER_RANGES["popup_rows"][1]


def test_a_popup_with_no_rows_is_raised_to_one(settings_file):
    settings_file.write_text(json.dumps({"popup_rows": 0}), encoding="utf-8")
    assert config.load_config()["popup_rows"] == 1


def test_every_numeric_setting_has_a_default(settings_file):
    # A range without a matching default would fall over the moment the setting was missing or wrong.
    assert set(config.NUMBER_RANGES) <= set(config.DEFAULT_CONFIG)


def test_a_negative_age_cutoff_becomes_no_cutoff(settings_file):
    settings_file.write_text(json.dumps({"max_age_days": -5}), encoding="utf-8")
    assert config.load_config()["max_age_days"] == 0


def test_a_non_numeric_interval_falls_back_to_the_default(settings_file):
    settings_file.write_text(json.dumps({"poll_minutes": "soon"}), encoding="utf-8")
    assert config.load_config()["poll_minutes"] == config.DEFAULT_CONFIG["poll_minutes"]


def test_text_settings_are_trimmed(settings_file):
    settings_file.write_text(json.dumps({"dashboard_command": "  my-dash  "}), encoding="utf-8")
    assert config.load_config()["dashboard_command"] == "my-dash"


def test_a_null_setting_is_treated_as_blank(settings_file):
    settings_file.write_text(json.dumps({"dashboard_command": None}), encoding="utf-8")
    assert config.load_config()["dashboard_command"] == ""


def test_saving_then_loading_round_trips(settings_file):
    stored = config.load_config()
    stored["dashboard_command"] = "my-dash"
    stored["toasts"]["conflict"] = True
    config.save_config(stored)
    reloaded = config.load_config()
    assert reloaded["dashboard_command"] == "my-dash"
    assert reloaded["toasts"]["conflict"] is True


def test_a_setting_left_over_from_an_older_version_is_dropped(settings_file):
    # The collector used to be a shell script with its own paths, and those settings mean nothing now.
    settings_file.write_text(
        json.dumps({"collector": "/somewhere/digest.sh", "bash_path": "/bin/bash", "orgs": "acme"}), encoding="utf-8"
    )
    loaded = config.load_config()
    assert not {"collector", "bash_path", "orgs"} & set(loaded)


def test_hidden_organisations_are_read_from_a_list_or_a_hand_written_string():
    assert config.login_list(["acme", "widgets"]) == ["acme", "widgets"]
    assert config.login_list("acme, @widgets acme") == ["acme", "widgets"]
    assert config.login_list(None) == []


def test_hidden_organisations_are_kept_through_a_round_trip(settings_file):
    config.save_config({**config.DEFAULT_CONFIG, "hidden_owners": "acme widgets"})
    assert config.load_config()["hidden_owners"] == ["acme", "widgets"]
