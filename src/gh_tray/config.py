"""Settings storage, defaults and data locations; paths are discovered at runtime, not hardcoded to one machine."""

from __future__ import annotations

import copy

from loguru import logger
from platformdirs import user_data_path

from . import APP_NAME
from .storage import read_json, write_json_atomic
from .theme import STYLES

APP_DIR = user_data_path(APP_NAME, appauthor=False)
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
SNAPSHOT_PATH = APP_DIR / "snapshot.json"
EVENTS_PATH = APP_DIR / "events.jsonl"
SEEN_PATH = APP_DIR / "seen.json"
LOG_PATH = APP_DIR / "gh-tray.log"
STDERR_PATH = APP_DIR / "gh-tray.stderr.log"
LOCK_PATH = APP_DIR / "gh-tray.lock"
ERROR_LOG_PATH = APP_DIR / "last_error.log"
APP_ICON_PATH = APP_DIR / "gh-tray.png"
LAYOUT_PATH = APP_DIR / "layout.ini"

DEFAULT_CONFIG: dict = {
    "dashboard_command": "",
    "poll_minutes": 10,
    "max_age_days": 365,
    "popup_rows": 20,
    "hidden_owners": [],
    "watch_others": True,
    "watched_owners": [],
    "involved": False,
    "theme": "auto",
    "opacity": None,
    "blur": True,
    "toasts": {
        "review_requested": True,
        "ci_broken": True,
        "changes_requested": True,
        "ready_to_merge": True,
        "mention": True,
        "conflict": False,
        "new_comment": False,
    },
}

TEXT_KEYS = ("dashboard_command",)
HIDDEN_OWNERS_KEY = "hidden_owners"
# Recorded because the collector cannot see the owner list without asking GitHub.
WATCH_OTHERS_KEY = "watch_others"
WATCHED_OWNERS_KEY = "watched_owners"
INVOLVED_KEY = "involved"
THEME_KEY = "theme"
# How solid the changes window's background is, in percent. Unset, it depends on whether the desktop blurs
# what lies behind the window: a blurred background can be more see-through and still read.
OPACITY_KEY = "opacity"
# Whether to ask the desktop to blur behind the window, where it can.
BLUR_KEY = "blur"
OPACITY_RANGE = (40, 100)
PLAIN_OPACITY = 95
BLURRED_OPACITY = 80

NUMBER_RANGES: dict[str, tuple[int, int | None]] = {
    "poll_minutes": (1, None),
    "max_age_days": (0, None),
    "popup_rows": (1, 50),
}


def login_list(value: object) -> list[str]:
    """Return a list of GitHub logins from a setting, however it was written.

    :param value: the setting as read, possibly a comma- or space-separated string with duplicates or a leading @
    """
    parts = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    logins: list[str] = []
    for part in parts:
        login = str(part).strip().lstrip("@")
        if login and login.casefold() not in {kept.casefold() for kept in logins}:
            logins.append(login)
    return logins


def normalise(config: dict) -> dict:
    """Coerce a settings mapping into usable values, so a hand-edited file cannot stop the tray starting.

    :param config: settings as read from disk, possibly with wrong types or out-of-range numbers
    :return: the same mapping with numbers clamped and text fields forced to strings
    """
    for key, (minimum, maximum) in NUMBER_RANGES.items():
        try:
            value = max(minimum, int(config[key]))
        except (TypeError, ValueError, KeyError):
            logger.warning("setting {} is not a whole number, using the default", key)
            value = DEFAULT_CONFIG[key]
        config[key] = min(value, maximum) if maximum is not None else value
    for key in TEXT_KEYS:
        config[key] = str(config.get(key) or "").strip()
    if config.get(OPACITY_KEY) is not None:
        try:
            config[OPACITY_KEY] = min(max(OPACITY_RANGE[0], int(config[OPACITY_KEY])), OPACITY_RANGE[1])
        except (TypeError, ValueError):
            logger.warning("setting {} is not a whole number, leaving it unset", OPACITY_KEY)
            config[OPACITY_KEY] = None
    config[HIDDEN_OWNERS_KEY] = login_list(config.get(HIDDEN_OWNERS_KEY))
    config[WATCHED_OWNERS_KEY] = login_list(config.get(WATCHED_OWNERS_KEY))
    config[WATCH_OTHERS_KEY] = bool(config.get(WATCH_OTHERS_KEY, DEFAULT_CONFIG[WATCH_OTHERS_KEY]))
    config[INVOLVED_KEY] = bool(config.get(INVOLVED_KEY, DEFAULT_CONFIG[INVOLVED_KEY]))
    config[BLUR_KEY] = bool(config.get(BLUR_KEY, DEFAULT_CONFIG[BLUR_KEY]))
    config["toasts"] = {
        kind: bool(config["toasts"].get(kind, default)) for kind, default in DEFAULT_CONFIG["toasts"].items()
    }
    if config.get(THEME_KEY) not in STYLES:
        config[THEME_KEY] = DEFAULT_CONFIG[THEME_KEY]
    return config


def merge_stored(config: dict, stored: object) -> dict:
    """Fold a settings document read from disk into the defaults, ignoring anything of the wrong shape.

    Bad values are dropped with a warning, not raised, since crashing here would also block the settings window.

    :param config: the defaults, modified in place
    :param stored: whatever was parsed out of the settings file
    :return: the merged settings
    """
    if not isinstance(stored, dict):
        logger.error("settings file does not hold a set of settings, falling back to defaults")
        return config
    for key, value in stored.items():
        if key == "toasts" or key not in config:
            continue
        config[key] = value
    toasts = stored.get("toasts")
    if isinstance(toasts, dict):
        config["toasts"].update(toasts)
    elif toasts is not None:
        logger.warning("the notification settings are not a set of switches, falling back to defaults")
    return config


def default_opacity(blurred: bool) -> int:
    """Return how solid the changes window is when the settings do not say.

    :param blurred: whether the desktop blurs what lies behind the window
    """
    return BLURRED_OPACITY if blurred else PLAIN_OPACITY


def load_config() -> dict:
    """Return the stored settings, filling in any key the settings file does not carry or carries wrongly."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    stored, _damaged = read_json(CONFIG_PATH)
    if stored is not None:
        config = merge_stored(config, stored)
    return normalise(config)


def save_config(config: dict) -> None:
    """Write settings back to disk, creating the application directory if needed."""
    write_json_atomic(CONFIG_PATH, normalise(config), indent=2)
