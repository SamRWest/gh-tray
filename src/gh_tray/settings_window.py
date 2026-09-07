"""The settings window: polling, notifications, dashboard command, owners, colours, login start and sign-in."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from loguru import logger
from PySide6.QtCore import QObject, Qt, Signal
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
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME
from .config import (
    APP_ICON_PATH,
    BLUR_KEY,
    HIDDEN_OWNERS_KEY,
    INVOLVED_KEY,
    NUMBER_RANGES,
    OPACITY_KEY,
    OPACITY_RANGE,
    THEME_KEY,
    WATCH_OTHERS_KEY,
    WATCHED_OWNERS_KEY,
    default_opacity,
    load_config,
    save_config,
)
from .environment import autostart_enabled, github_auth_state, hide_from_dock, open_in_terminal, set_autostart
from .events import RULE_LABELS
from .github import GitHubError, organisations, viewer
from .status import write_app_icon
from .theme import ALWAYS_DARK, ALWAYS_LIGHT, FOLLOW_DESKTOP, chosen_style, ink, palette
from .toolkit import FontZoom, application, compositing_available, follow_theme_setting, layout_store

# Ranges come from the settings module, so the window cannot accept a value the settings would clamp anyway.
NUMBER_FIELDS = {
    "poll_minutes": "Poll every (minutes)",
    "max_age_days": "Hide pull requests older than (days, 0 = keep all)",
    "popup_rows": "Changes shown when you click the tray icon",
}
# A spin box needs a ceiling; a setting with none gets one nobody will reach.
UNBOUNDED = 100_000

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
    """Looks the account up off the GUI thread and answers back on it, where widgets are safe to touch."""

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
    """The settings window. Saves settings and login on close; account details arrive later over a signal."""

    # The opacity slider and blur switch are followed live by the changes window, so the effect is seen before
    # it is saved.
    opacity_changed = Signal(int)
    blur_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None, account: Account | None = None, blurrable: bool = False) -> None:
        """Build the window around the settings as they stand.

        :param parent: the window this one belongs to, if any
        :param account: what is already known about the account, or None to look it up now
        :param blurrable: whether the desktop can blur behind the changes window
        """
        super().__init__(parent)
        self.blurrable = blurrable
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
        form.addRow("Blur behind it", self.blur_switch())
        form.addRow("Window opacity", self.opacity_slider())
        self.involved = QCheckBox("Pull requests you only commented on or were assigned", self)
        self.involved.setChecked(bool(self.config.get(INVOLVED_KEY)))
        form.addRow("Also list", self.involved)
        return form

    def blur_switch(self) -> QCheckBox:
        """Lay out the switch for the desktop's blur behind the changes window, greyed where there is none."""
        self.blur = QCheckBox("If supported (Windows 11, macOS, KDE)", self)
        self.blur.setChecked(bool(self.config.get(BLUR_KEY, True)))
        self.blur.setEnabled(self.blurrable)
        if not self.blurrable:
            self.blur.setToolTip("This desktop does not blur behind windows.")
        self.blur.toggled.connect(self.on_blur_switched)
        return self.blur

    def on_blur_switched(self, wanted: bool) -> None:
        """Pass the blur switch on, and move an untouched opacity slider to the default for the new state.

        :param wanted: whether the blur should be drawn
        """
        self.blur_changed.emit(wanted)
        if self.config.get(OPACITY_KEY) is None and not self.opacity_moved:
            self.opacity.setValue(default_opacity(self.blurrable and wanted))
            self.opacity_moved = False

    def opacity_slider(self) -> QHBoxLayout:
        """Lay out the slider for how solid the changes window is, with its value beside it."""
        self.opacity = QSlider(Qt.Orientation.Horizontal, self)
        self.opacity.setRange(*OPACITY_RANGE)
        stored = self.config.get(OPACITY_KEY)
        self.opacity.setValue(
            int(stored) if stored is not None else default_opacity(self.blurrable and self.blur.isChecked())
        )
        self.opacity_moved = False
        self.opacity_value = QLabel(f"{self.opacity.value()}%", self)
        self.opacity.valueChanged.connect(self.on_opacity_moved)
        if not compositing_available():
            self.opacity.setEnabled(False)
            self.opacity.setToolTip("This desktop cannot draw see-through windows.")
        row = QHBoxLayout()
        row.addWidget(self.opacity, 1)
        row.addWidget(self.opacity_value)
        return row

    def on_opacity_moved(self, value: int) -> None:
        """Show the slider's value and pass it on.

        :param value: how solid the window should be, in percent
        """
        self.opacity_value.setText(f"{value}%")
        self.opacity_moved = True
        self.opacity_changed.emit(value)

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
        """Lay out the owner switches. Only turned-off owners are stored, so new orgs are watched and old repos kept."""
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
        """Fill in what GitHub reported: an owner switch (kept after leaving, to re-enable) and the sign-in state.

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
        self.config[OPACITY_KEY] = self.opacity.value()
        self.config[BLUR_KEY] = self.blur.isChecked()
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
