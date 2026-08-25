"""Settings storage, defaults and the application's data locations.

Every path is either discovered at runtime or held in the settings file, so nothing about one machine or one account
is written into the code.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from platformdirs import user_data_path

from . import APP_NAME
from .environment import detect_orgs

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

MINIMUM_POLL_MINUTES = 1
TEXT_KEYS = ("orgs", "collector", "bash_path", "dashboard_command")


def normalise(config: dict) -> dict:
    """Coerce a settings mapping into usable values, so a hand-edited file cannot stop the tray starting.

    :param config: settings as read from disk, possibly with wrong types or out-of-range numbers
    :return: the same mapping with numbers clamped and text fields forced to strings
    """
    for key, minimum in (("poll_minutes", MINIMUM_POLL_MINUTES), ("max_age_days", 0)):
        try:
            config[key] = max(minimum, int(config[key]))
        except (TypeError, ValueError):
            logger.warning("setting {} is not a whole number, using the default", key)
            config[key] = DEFAULT_CONFIG[key]
    for key in TEXT_KEYS:
        config[key] = str(config.get(key) or "").strip()
    config["toasts"] = {kind: bool(config["toasts"].get(kind, default)) for kind, default in DEFAULT_CONFIG["toasts"].items()}
    return config


def load_config() -> dict:
    """Return the stored settings, filling in any key the settings file does not carry."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.error("settings file is not readable JSON, falling back to defaults: {}", CONFIG_PATH)
            return normalise(config)
        config.update({key: value for key, value in stored.items() if key != "toasts"})
        config["toasts"].update(stored.get("toasts", {}))
    return normalise(config)


def save_config(config: dict) -> None:
    """Write settings back to disk, creating the application directory if needed."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(normalise(config), indent=2) + "\n", encoding="utf-8")


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
