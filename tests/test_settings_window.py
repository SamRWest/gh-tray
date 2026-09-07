"""The settings window: what it shows from the stored settings and the account, and what it writes back on save."""

from __future__ import annotations

import copy

import pytest
from pytestqt.qtbot import QtBot

from gh_tray import config, settings_window, theme
from gh_tray.settings_window import Account, SettingsDialog

TESTER = Account(
    login="tester", organisations=("acme", "widgets"), signed_in=True, sign_in_summary="Signed in as tester"
)


class DialogBuilder:
    """Builds real SettingsDialog instances with the settings file stubbed out, recording what each writes."""

    def __init__(self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
        """:param qtbot: registers each built dialog for teardown."""
        self._qtbot = qtbot
        self._monkeypatch = monkeypatch
        self.saved: list[dict] = []
        self.autostart_calls: list[bool] = []
        self.theme_calls: list[str] = []
        monkeypatch.setattr(settings_window, "save_config", self.saved.append)
        monkeypatch.setattr(settings_window, "set_autostart", self.autostart_calls.append)
        monkeypatch.setattr(settings_window, "follow_theme_setting", self.theme_calls.append)

    def __call__(self, stored: dict, autostart: bool = False, account: Account | None = TESTER) -> SettingsDialog:
        """Build one dialog showing the given stored settings.

        :param stored: the settings the dialog should load
        :param autostart: whether login start should read as already on
        :param account: what the dialog is told about the account, or None to leave it waiting
        """
        self._monkeypatch.setattr(settings_window, "load_config", lambda: copy.deepcopy(stored))
        self._monkeypatch.setattr(settings_window, "autostart_enabled", lambda: autostart)
        if account is None:
            self._monkeypatch.setattr(settings_window.AccountLookup, "start", lambda _self: None)
        dialog = SettingsDialog(account=account)
        self._qtbot.addWidget(dialog)
        return dialog


@pytest.fixture
def build_dialog(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> DialogBuilder:
    """Return a builder for SettingsDialog instances."""
    return DialogBuilder(qtbot, monkeypatch)


def test_the_dialog_shows_the_stored_numbers_command_switches_theme_and_autostart(build_dialog):
    stored = copy.deepcopy(config.DEFAULT_CONFIG)
    stored.update(poll_minutes=15, max_age_days=7, popup_rows=12, dashboard_command="gh dash --repo acme/widget")
    stored[config.THEME_KEY] = theme.ALWAYS_DARK
    stored["toasts"]["ci_broken"] = False
    dialog = build_dialog(stored, autostart=True)
    assert dialog.numbers["poll_minutes"].value() == 15
    assert dialog.numbers["max_age_days"].value() == 7
    assert dialog.numbers["popup_rows"].value() == 12
    assert dialog.dashboard.text() == "gh dash --repo acme/widget"
    assert dialog.toggles["ci_broken"].isChecked() is False
    assert dialog.toggles["mention"].isChecked() is True
    assert dialog.style_buttons[theme.ALWAYS_DARK].isChecked() is True
    assert dialog.autostart.isChecked() is True
    assert dialog.sign_in.text() == "Signed in as tester"


def test_save_and_close_writes_the_settings_through_save_config_and_toggles_autostart(build_dialog):
    dialog = build_dialog(copy.deepcopy(config.DEFAULT_CONFIG), autostart=False)
    dialog.numbers["poll_minutes"].setValue(30)
    dialog.dashboard.setText("gh dash --repo acme/gadget")
    dialog.toggles["mention"].setChecked(False)
    dialog.style_buttons[theme.ALWAYS_LIGHT].setChecked(True)
    dialog.autostart.setChecked(True)
    dialog.save_and_close()
    written = build_dialog.saved[-1]
    assert written["poll_minutes"] == 30
    assert written["dashboard_command"] == "gh dash --repo acme/gadget"
    assert written["toasts"]["mention"] is False
    assert written[config.THEME_KEY] == theme.ALWAYS_LIGHT
    assert build_dialog.autostart_calls == [True]


def test_chosen_theme_follows_the_radio_buttons(build_dialog):
    dialog = build_dialog(copy.deepcopy(config.DEFAULT_CONFIG))
    dialog.style_buttons[theme.ALWAYS_DARK].setChecked(True)
    assert dialog.chosen_theme() == theme.ALWAYS_DARK
    dialog.style_buttons[theme.FOLLOW_DESKTOP].setChecked(True)
    assert dialog.chosen_theme() == theme.FOLLOW_DESKTOP


def test_the_account_and_every_organisation_are_listed_and_on_unless_turned_off(build_dialog):
    dialog = build_dialog({**config.DEFAULT_CONFIG, "hidden_owners": ["widgets", "former"]})
    assert list(dialog.owner_switches_by_login) == ["tester", "acme", "widgets", "former"]
    assert [switch.isChecked() for switch in dialog.owner_switches_by_login.values()] == [True, True, False, False]
    assert dialog.owner_switches_by_login["tester"].text() == "tester (your own repositories)"
    assert dialog.owner_note.isHidden()


def test_turning_an_owner_off_is_what_is_saved(build_dialog):
    dialog = build_dialog(copy.deepcopy(config.DEFAULT_CONFIG))
    dialog.owner_switches_by_login["acme"].setChecked(False)
    dialog.owner_switches_by_login["tester"].setChecked(False)
    dialog.save_and_close()
    assert build_dialog.saved[-1]["hidden_owners"] == ["tester", "acme"]


def test_a_failure_to_list_organisations_is_said_rather_than_raised(build_dialog):
    refused = Account(signed_in=False, sign_in_summary="Not signed in to GitHub", trouble="GitHub did not answer")
    dialog = build_dialog({**config.DEFAULT_CONFIG, "hidden_owners": ["widgets"]}, account=refused)
    assert list(dialog.owner_switches_by_login) == ["widgets"]
    assert dialog.owner_note.text() == "Could not list your organisations: GitHub did not answer"


def test_the_dialog_comes_up_waiting_and_fills_in_when_the_account_arrives(build_dialog):
    dialog = build_dialog(copy.deepcopy(config.DEFAULT_CONFIG), account=None)
    assert dialog.owner_note.text() == settings_window.LOOKING_UP_OWNERS
    assert dialog.sign_in.text() == settings_window.CHECKING_SIGN_IN
    assert dialog.owner_switches_by_login == {}
    dialog.take_account(TESTER)
    assert list(dialog.owner_switches_by_login) == ["tester", "acme", "widgets"]
    assert dialog.sign_in.text() == "Signed in as tester"


def test_look_up_account_gathers_the_sign_in_state_login_and_organisations(monkeypatch):
    monkeypatch.setattr(settings_window, "github_auth_state", lambda: (True, "Logged in as tester"))
    monkeypatch.setattr(settings_window, "viewer", lambda: "tester")
    monkeypatch.setattr(settings_window, "organisations", lambda: ["acme"])
    assert settings_window.look_up_account() == Account("tester", ("acme",), True, "Logged in as tester", "")


def test_the_involved_switch_and_the_catch_all_are_shown_and_saved(build_dialog):
    dialog = build_dialog({**config.DEFAULT_CONFIG, "involved": True, "watch_others": False})
    assert dialog.involved.isChecked() and not dialog.others.isChecked()
    dialog.involved.setChecked(False)
    dialog.others.setChecked(True)
    dialog.owner_switches_by_login["widgets"].setChecked(False)
    dialog.save_and_close()
    saved = build_dialog.saved[-1]
    assert saved["involved"] is False
    assert saved["watch_others"] is True
    assert saved["watched_owners"] == ["tester", "acme"]
    assert saved["hidden_owners"] == ["widgets"]
