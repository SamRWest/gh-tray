"""Settings storage, defaults and the application's data locations.

Every path is either discovered at runtime or held in the settings file, so nothing about one machine or one account
is written into the code.
"""

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
LOCK_PATH = APP_DIR / "gh-tray.lock"
ERROR_LOG_PATH = APP_DIR / "last_error.log"
# Drawn once and kept, since the desktop wants a file on disk rather than a picture in memory.
APP_ICON_PATH = APP_DIR / "gh-tray.png"
# Left behind by a window asking the tray to poll now, since the two are separate processes and this is the whole
# of what one needs to say to the other.
REFRESH_REQUEST_PATH = APP_DIR / "refresh.request"
# The same the other way round: the tray asking the changes window to show itself. That window stays loaded and
# hidden between showings, so being asked reaches it in a few milliseconds rather than the second a fresh process
# takes to start. Its lock is what says one is already waiting.
POPUP_REQUEST_PATH = APP_DIR / "popup.request"
POPUP_LOCK_PATH = APP_DIR / "popup.lock"
# The size the user last dragged the changes window to, and its column widths, so both survive a restart.
LAYOUT_PATH = APP_DIR / "layout.json"

# A blank dashboard command means "work it out at runtime", using whichever terminal this platform provides.
DEFAULT_CONFIG: dict = {
    "dashboard_command": "",
    "poll_minutes": 10,
    "max_age_days": 365,
    "popup_rows": 20,
    "theme": "auto",
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
# The theme the windows are drawn in: follow the desktop, or insist on one.
THEME_KEY = "theme"

# Each numeric setting and the range it must fall in. A popup taller than this stops being a popup, and a poll
# interval below a minute would hammer the GitHub API for no benefit.
NUMBER_RANGES: dict[str, tuple[int, int | None]] = {
    "poll_minutes": (1, None),
    "max_age_days": (0, None),
    "popup_rows": (1, 50),
}


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
    config["toasts"] = {
        kind: bool(config["toasts"].get(kind, default)) for kind, default in DEFAULT_CONFIG["toasts"].items()
    }
    if config.get(THEME_KEY) not in STYLES:
        config[THEME_KEY] = DEFAULT_CONFIG[THEME_KEY]
    return config


def merge_stored(config: dict, stored: object) -> dict:
    """Fold a settings document read from disk into the defaults, ignoring anything of the wrong shape.

    A settings file is hand-editable, so it can hold valid JSON that is nonetheless the wrong type. Every such value
    is dropped with a warning rather than raised, because the settings window is the way to repair the file and a
    settings error that stops the application starting also stops that window opening.

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
