"""The settings window: polling, notification rules, the dashboard command, colours, login start and GitHub sign-in."""

from __future__ import annotations

from loguru import logger
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME
from .config import (
    APP_ICON_PATH,
    HIDDEN_OWNERS_KEY,
    INVOLVED_KEY,
    NUMBER_RANGES,
    THEME_KEY,
    WATCH_OTHERS_KEY,
    WATCHED_OWNERS_KEY,
    load_config,
    save_config,
)
from .environment import autostart_enabled, github_auth_summary, hide_from_dock, open_in_terminal, set_autostart
from .events import RULE_LABELS
from .github import GitHubError, organisations, viewer
from .prerequisites import signed_in
from .status import write_app_icon
from .theme import ALWAYS_DARK, ALWAYS_LIGHT, FOLLOW_DESKTOP, chosen_style, ink, palette
from .toolkit import FontZoom, application, follow_theme_setting, layout_store

# The numeric settings and what each is called in the window. Their ranges come from the settings module, so the
# window cannot accept what the settings would then clamp.
NUMBER_FIELDS = {
    "poll_minutes": "Poll every (minutes)",
    "max_age_days": "Hide pull requests older than (days, 0 = keep all)",
    "popup_rows": "Changes shown when you click the tray icon",
}
# A spin box has to have a ceiling; a setting that has none is given one nobody will reach.
UNBOUNDED = 100_000

# The theme choices offered, and what each is called in the window.
THEME_CHOICES = ((FOLLOW_DESKTOP, "Follow the desktop"), (ALWAYS_DARK, "Dark"), (ALWAYS_LIGHT, "Light"))


class SettingsDialog(QDialog):
    """The settings window. Saving writes the settings file and the login entry, and applies the colours at once."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the window around the settings as they stand.

        :param parent: the window this one belongs to, if any
        """
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} settings")
        try:
            self.setWindowIcon(QIcon(str(write_app_icon(APP_ICON_PATH))))
        except OSError as error:
            logger.debug("could not put the application's mark on the settings window: {}", error)
        self.config = load_config()
        column = QVBoxLayout(self)
        column.addLayout(self.fields())
        column.addWidget(self.notification_switches())
        column.addWidget(self.owner_switches())
        column.addWidget(self.colour_choices())
        self.autostart = QCheckBox("Start automatically at login", self)
        self.autostart.setChecked(autostart_enabled())
        column.addWidget(self.autostart)
        column.addWidget(self.sign_in_state())
        column.addWidget(self.buttons())

    def fields(self) -> QFormLayout:
        """Lay out the numbers and the dashboard command."""
        form = QFormLayout()
        self.numbers: dict[str, QSpinBox] = {}
        for key, label in NUMBER_FIELDS.items():
            minimum, maximum = NUMBER_RANGES[key]
            spin = QSpinBox(self)
            spin.setRange(minimum, maximum if maximum is not None else UNBOUNDED)
            spin.setValue(int(self.config[key]))
            form.addRow(label, spin)
            self.numbers[key] = spin
        self.dashboard = QLineEdit(str(self.config["dashboard_command"]), self)
        self.dashboard.setPlaceholderText("gh dash")
        form.addRow("Dashboard command", self.dashboard)
        self.involved = QCheckBox("Pull requests you are involved in some other way, as the dashboard lists them", self)
        self.involved.setChecked(bool(self.config.get(INVOLVED_KEY)))
        form.addRow("Also list", self.involved)
        return form

    def notification_switches(self) -> QGroupBox:
        """Lay out one switch per kind of change that can raise a notification."""
        group = QGroupBox("Notify me about", self)
        column = QVBoxLayout(group)
        self.toggles: dict[str, QCheckBox] = {}
        for kind, (label, _urgent) in RULE_LABELS.items():
            switch = QCheckBox(label, group)
            switch.setChecked(bool(self.config["toasts"].get(kind)))
            column.addWidget(switch)
            self.toggles[kind] = switch
        return group

    def owner_switches(self) -> QGroupBox:
        """Lay out a switch per owner, the account itself and each organisation, on unless the user turned it off.

        The owners are the account itself and every organisation it belongs to. Off rather than on is what is
        remembered, so an organisation joined later is watched without a visit here, and pull requests in a
        repository the account merely contributes to from outside are never lost. One turned off stays listed after
        the account leaves it, so it can be turned on again.
        """
        group = QGroupBox("Watch pull requests in", self)
        column = QVBoxLayout(group)
        # The catch-all first: every owner not listed below. Off, only the owners ticked below are watched.
        self.others = QCheckBox("Anything else you have a hand in", group)
        self.others.setChecked(bool(self.config.get(WATCH_OTHERS_KEY, True)))
        column.addWidget(self.others)
        hidden = [str(login) for login in self.config.get(HIDDEN_OWNERS_KEY) or []]
        try:
            own = viewer()
            known = ([own] if own else []) + organisations()
        except GitHubError as error:
            own, known = "", []
            column.addWidget(QLabel(f"Could not list your organisations: {error}", group))
        listed = known + [login for login in hidden if login.casefold() not in {name.casefold() for name in known}]
        self.owner_switches_by_login: dict[str, QCheckBox] = {}
        for login in listed:
            switch = QCheckBox(f"{login} (your own repositories)" if login == own else login, group)
            switch.setChecked(login.casefold() not in {name.casefold() for name in hidden})
            column.addWidget(switch)
            self.owner_switches_by_login[login] = switch
        if not listed:
            column.addWidget(QLabel("GitHub named no owner. Everything you have a hand in is watched.", group))
        return group

    def colour_choices(self) -> QGroupBox:
        """Lay out the choice between following the desktop's theme and insisting on one."""
        group = QGroupBox("Colours", self)
        row = QHBoxLayout(group)
        self.styles = QButtonGroup(self)
        self.style_buttons: dict[str, QRadioButton] = {}
        for value, label in THEME_CHOICES:
            button = QRadioButton(label, group)
            button.setChecked(value == self.config[THEME_KEY])
            self.styles.addButton(button)
            row.addWidget(button)
            self.style_buttons[value] = button
        return group

    def sign_in_state(self) -> QLabel:
        """Say whether GitHub is signed in, in colour as well as in words, since that decides whether anything works."""
        inks = palette(chosen_style())
        state = QLabel(github_auth_summary(), self)
        state.setWordWrap(True)
        state.setStyleSheet(f"color: {ink(inks, 'green') if signed_in() else ink(inks, 'red')}")
        return state

    def buttons(self) -> QDialogButtonBox:
        """Lay out the buttons: sign in, cancel, and the one that commits, which stands out and answers Enter."""
        box = QDialogButtonBox(self)
        box.addButton("Sign in to GitHub", QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(self.sign_in)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        box.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole).setDefault(True)
        box.accepted.connect(self.save_and_close)
        box.rejected.connect(self.reject)
        return box

    def chosen_theme(self) -> str:
        """Return the theme the user has chosen."""
        return next((value for value, button in self.style_buttons.items() if button.isChecked()), FOLLOW_DESKTOP)

    def sign_in(self) -> None:
        """Launch an interactive GitHub sign-in in a terminal window."""
        try:
            open_in_terminal("gh auth login", "gh auth")
        except RuntimeError as error:
            QMessageBox.critical(self, APP_NAME, str(error))

    def save_and_close(self) -> None:
        """Persist the settings and close. Spin boxes admit only whole numbers in range, so nothing is checked."""
        for key, spin in self.numbers.items():
            self.config[key] = spin.value()
        self.config["dashboard_command"] = self.dashboard.text().strip()
        self.config["toasts"] = {kind: switch.isChecked() for kind, switch in self.toggles.items()}
        switches = self.owner_switches_by_login.items()
        self.config[HIDDEN_OWNERS_KEY] = [login for login, switch in switches if not switch.isChecked()]
        self.config[WATCHED_OWNERS_KEY] = [login for login, switch in switches if switch.isChecked()]
        self.config[WATCH_OTHERS_KEY] = self.others.isChecked()
        self.config[INVOLVED_KEY] = self.involved.isChecked()
        self.config[THEME_KEY] = self.chosen_theme()
        save_config(self.config)
        set_autostart(self.autostart.isChecked())
        follow_theme_setting(self.config[THEME_KEY])
        self.accept()


def run_settings() -> None:
    """Show the settings window on its own and block until it is closed."""
    application()
    hide_from_dock()
    follow_theme_setting(load_config()[THEME_KEY])
    zoom = FontZoom(layout_store())
    dialog = SettingsDialog()
    zoom.changed.connect(dialog.adjustSize)
    dialog.exec()
