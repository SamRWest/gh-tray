"""Settings storage, defaults and the application's data locations.

Every path is either discovered at runtime or held in the settings file, so nothing about one machine or one account
is written into the code.
"""

from __future__ import annotations

import copy
from pathlib import Path

from loguru import logger
from platformdirs import user_data_path

from . import APP_NAME
from .environment import detect_orgs
from .storage import read_json, write_json_atomic

PACKAGE_ROOT = Path(__file__).resolve().parent
BUNDLED_COLLECTOR = PACKAGE_ROOT / "data" / "digest.sh"

APP_DIR = user_data_path(APP_NAME, appauthor=False)
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
SNAPSHOT_PATH = APP_DIR / "snapshot.json"
EVENTS_PATH = APP_DIR / "events.jsonl"
SEEN_PATH = APP_DIR / "seen.json"
LOG_PATH = APP_DIR / "gh-tray.log"
LOCK_PATH = APP_DIR / "gh-tray.lock"
ERROR_LOG_PATH = APP_DIR / "last_error.log"

# Blank values mean "work it out at runtime": the collector shipped with this package, every organisation the
# signed-in account belongs to, whichever bash and terminal this platform provides.
DEFAULT_CONFIG: dict = {
    "collector": "",
    "bash_path": "",
    "dashboard_command": "",
    "poll_minutes": 10,
    "orgs": "",
    "max_age_days": 365,
    "popup_rows": 20,
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

TEXT_KEYS = ("orgs", "collector", "bash_path", "dashboard_command")

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
    config["toasts"] = {kind: bool(config["toasts"].get(kind, default)) for kind, default in DEFAULT_CONFIG["toasts"].items()}
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


def collector_path(config: dict) -> Path:
    """Return the collector script to run, defaulting to the copy shipped with this package."""
    return Path(config["collector"]).expanduser() if config.get("collector") else BUNDLED_COLLECTOR


def bootstrap() -> dict:
    """Load the settings, discovering the organisation list the first time so the app works before configuration."""
    first_run = not CONFIG_PATH.exists()
    config = load_config()
    if first_run:
        config["orgs"] = detect_orgs()
        save_config(config)
        logger.info("first run: organisations set to {}", config["orgs"] or "(none found)")
    return config
