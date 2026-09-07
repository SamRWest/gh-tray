"""The settings window: polling, notifications, dashboard command, owners, colours, login start and sign-in."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from loguru import logger
from PySide6.QtCore import QObject, Signal
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
from .environment import autostart_enabled, github_auth_state, hide_from_dock, open_in_terminal, set_autostart
from .events import RULE_LABELS
from .github import GitHubError, organisations, viewer
from .status import write_app_icon
from .theme import ALWAYS_DARK, ALWAYS_LIGHT, FOLLOW_DESKTOP, chosen_style, ink, palette
from .toolkit import FontZoom, application, follow_theme_setting, layout_store

# The numeric settings and their labels. Ranges come from the settings module, so the window cannot accept a value
# the settings would clamp anyway.
NUMBER_FIELDS = {
    "poll_minutes": "Poll every (minutes)",
    "max_age_days": "Hide pull requests older than (days, 0 = keep all)",
    "popup_rows": "Changes shown when you click the tray icon",
}
# A spin box needs a ceiling; a setting with none gets one nobody will reach.
UNBOUNDED = 100_000

# The theme choices offered, and their labels.
THEME_CHOICES = ((FOLLOW_DESKTOP, "Follow the desktop"), (ALWAYS_DARK, "Dark"), (ALWAYS_LIGHT, "Light"))

LOOKING_UP_OWNERS = "Looking up your organisations..."
CHECKING_SIGN_IN = "Checking GitHub sign-in..."
NO_OWNERS = "GitHub named no owner. Everything you have a hand in is watched."


@dataclass(frozen=True)
class Account:
    """What the window shows about the signed-in account, each part learned from GitHub."""

    login: str = ""
    organisations: tuple[str, ...] = ()
    signed_in: bool = False
    sign_in_summary: str = ""
    # Why the organisations could not be listed, when they could not.
    trouble: str = ""


def look_up_account() -> Account:
    """Ask GitHub about the account. Slow, so called off the toolkit's thread."""
    signed_in, summary = github_auth_state()
    try:
        login, listed, trouble = viewer(), tuple(organisations()), ""
    except GitHubError as error:
        login, listed, trouble = "", (), str(error)
    return Account(login, listed, signed_in, summary, trouble)


class AccountLookup(QObject):
    """Looks the account up on a thread of its own and hands the answer back over a signal.

    The toolkit delivers the signal on its own thread, where windows may be touched.
    """

    found = Signal(object)

    def start(self) -> None:
        """Look the account up, without waiting."""
        threading.Thread(target=self.run, daemon=True, name=f"{APP_NAME}-account").start()

    def run(self) -> None:
        """Look the account up and report it, reporting the failure instead if the lookup dies."""
        try:
            self.found.emit(look_up_account())
        except Exception as error:  # a window is waiting on the answer, so it has to get one
            logger.exception("looking up the account failed")
            self.found.emit(Account(trouble=str(error)[:100]))


class SettingsDialog(QDialog):
    """The settings window. Saving writes the settings file and the login entry, and applies the colours at once.

    The settings load immediately from the settings file. Account details, the owners to list and the sign-in state
    arrive later over a signal, unless already known, so the window never has to wait.
    """

    def __init__(self, parent: QWidget | None = None, account: Account | None = None) -> None:
        """Build the window around the settings as they stand.

        :param parent: the window this one belongs to, if any
        :param account: what is already known about the account, or None to look it up now
        """
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} settings")
        try:
            self.setWindowIcon(QIcon(str(write_app_icon(APP_ICON_PATH))))
        except OSError as error:
            logger.debug("could not set the settings window's icon: {}", error)
        self.config = load_config()
        self.owner_switches_by_login: dict[str, QCheckBox] = {}
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
        self.lookup = AccountLookup()
        self.lookup.found.connect(self.take_account)
        if account is None:
            self.lookup.start()
        else:
            self.take_account(account)

    def fields(self) -> QFormLayout:
        """Lay out the numbers, the dashboard command and the Also list switch."""
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
        self.involved = QCheckBox("Pull requests you only commented on or were assigned", self)
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
        """Lay out the catch-all owner switch, with room below it for one switch per owner once GitHub lists them.

        Only turned-off owners are remembered. An organisation joined later is then watched without a visit here,
        and a repository the account merely contributes to is never lost.
        """
        group = QGroupBox("Repository owners to watch", self)
        self.owner_column = QVBoxLayout(group)
        self.others = QCheckBox("Any other owner not listed here", group)
        self.others.setChecked(bool(self.config.get(WATCH_OTHERS_KEY, True)))
        self.owner_column.addWidget(self.others)
        self.owner_note = QLabel(LOOKING_UP_OWNERS, group)
        self.owner_note.setWordWrap(True)
        self.owner_column.addWidget(self.owner_note)
        return group

    def take_account(self, account: Account) -> None:
        """Fill in what GitHub reported: a switch per owner, and the sign-in state.

        Owners are the account itself plus every organisation it belongs to. A switch turned off stays listed even
        after the account leaves that organisation, so it can be turned back on.

        :param account: what GitHub reported
        """
        hidden = [str(login) for login in self.config.get(HIDDEN_OWNERS_KEY) or []]
        known = ([account.login] if account.login else []) + list(account.organisations)
        listed = known + [login for login in hidden if login.casefold() not in {name.casefold() for name in known}]
        for login in listed:
            text = f"{login} (your own repositories)" if login == account.login else login
            switch = QCheckBox(text, self.owner_note.parentWidget())
            switch.setChecked(login.casefold() not in {name.casefold() for name in hidden})
            self.owner_column.addWidget(switch)
            self.owner_switches_by_login[login] = switch
        if account.trouble:
            self.owner_note.setText(f"Could not list your organisations: {account.trouble}")
        elif listed:
            self.owner_note.hide()
        else:
            self.owner_note.setText(NO_OWNERS)
        inks = palette(chosen_style())
        self.sign_in.setText(account.sign_in_summary)
        self.sign_in.setStyleSheet(f"color: {ink(inks, 'green' if account.signed_in else 'red')}")
        self.adjustSize()

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
        """Lay out the line that says whether GitHub is signed in, coloured once that is known."""
        self.sign_in = QLabel(CHECKING_SIGN_IN, self)
        self.sign_in.setWordWrap(True)
        return self.sign_in

    def buttons(self) -> QDialogButtonBox:
        """Lay out the buttons: sign in, cancel, and Save, which stands out and answers Enter."""
        box = QDialogButtonBox(self)
        box.addButton("Sign in to GitHub", QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(self.start_sign_in)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        box.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole).setDefault(True)
        box.accepted.connect(self.save_and_close)
        box.rejected.connect(self.reject)
        return box

    def chosen_theme(self) -> str:
        """Return the theme the user has chosen."""
        return next((value for value, button in self.style_buttons.items() if button.isChecked()), FOLLOW_DESKTOP)

    def start_sign_in(self) -> None:
        """Start an interactive GitHub sign-in in a terminal window."""
        try:
            open_in_terminal("gh auth login", "gh auth")
        except RuntimeError as error:
            QMessageBox.critical(self, APP_NAME, str(error))

    def save_and_close(self) -> None:
        """Write the settings and close. Spin boxes admit only whole numbers in range, so nothing is checked."""
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
