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
    assert loaded["orgs"] == ""


def test_stored_values_win_over_defaults(settings_file):
    settings_file.write_text(json.dumps({"poll_minutes": 3, "orgs": "acme"}), encoding="utf-8")
    loaded = config.load_config()
    assert loaded["poll_minutes"] == 3
    assert loaded["orgs"] == "acme"


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
    settings_file.write_text(json.dumps({"orgs": "  acme , widget "}), encoding="utf-8")
    assert config.load_config()["orgs"] == "acme , widget"


def test_a_null_path_is_treated_as_blank(settings_file):
    settings_file.write_text(json.dumps({"bash_path": None}), encoding="utf-8")
    assert config.load_config()["bash_path"] == ""


def test_saving_then_loading_round_trips(settings_file):
    stored = config.load_config()
    stored["orgs"] = "acme"
    stored["toasts"]["conflict"] = True
    config.save_config(stored)
    reloaded = config.load_config()
    assert reloaded["orgs"] == "acme"
    assert reloaded["toasts"]["conflict"] is True


def test_the_bundled_collector_is_used_when_no_path_is_set():
    assert config.collector_path({"collector": ""}) == config.BUNDLED_COLLECTOR


def test_a_named_collector_is_used_when_one_is_set(tmp_path):
    assert config.collector_path({"collector": str(tmp_path / "other.sh")}) == tmp_path / "other.sh"


def test_the_bundled_collector_ships_with_the_package():
    assert config.BUNDLED_COLLECTOR.exists()


def test_the_first_run_fills_in_the_organisations(settings_file, monkeypatch):
    monkeypatch.setattr(config, "detect_orgs", lambda: "acme,widget")
    assert config.bootstrap()["orgs"] == "acme,widget"
    assert settings_file.exists()


def test_a_later_run_leaves_the_organisations_alone(settings_file, monkeypatch):
    settings_file.write_text(json.dumps({"orgs": "chosen"}), encoding="utf-8")
    monkeypatch.setattr(config, "detect_orgs", lambda: "acme,widget")
    assert config.bootstrap()["orgs"] == "chosen"
