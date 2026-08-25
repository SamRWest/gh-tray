"""Running the collector script and turning its output into a digest.

The collector gathers everything in one pass and prints a single JSON document. It keeps its own baseline file,
passed as the first argument, so this application polling on a timer never consumes changes that another caller
would otherwise report.
"""

from __future__ import annotations

import json
import subprocess

from loguru import logger

from .config import APP_DIR, ERROR_LOG_PATH, STATE_PATH, collector_path
from .environment import find_bash, hidden_window_flags
from .storage import write_text_atomic

COLLECTOR_TIMEOUT_SECONDS = 180


def describe_failure(stderr: str) -> str:
    """Pick the most informative line out of a failed collector run.

    The collector's last line is often generic tool usage help, so the first line mentioning an error is preferred.

    :param stderr: everything the collector wrote to its error stream
    :return: a short single-line description
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "collector failed"
    described = next((line for line in lines if "error" in line.lower()), lines[0])
    return described[:120]


def record_failure(description: str, detail: str = "") -> tuple[None, str]:
    """Log a failed collection and keep the full detail for diagnosis.

    The record is written on every failure, including those detected before the collector runs, so it can never be
    mistaken for a stale record of some earlier and unrelated problem.

    :param description: the single line shown to the user
    :param detail: everything worth keeping, defaulting to the description itself
    :return: the failure in the shape :func:`run_digest` returns
    """
    logger.error("collection failed: {}", description)
    write_text_atomic(ERROR_LOG_PATH, detail or description)
    return None, description[:120]


def run_digest(config: dict) -> tuple[dict | None, str]:
    """Run the collector and return its digest.

    :param config: current settings, supplying the collector path, bash path, organisations and age cutoff
    :return: the digest and an empty string on success, or None and a description of the failure
    """
    bash = find_bash(config.get("bash_path", ""))
    if not bash:
        return record_failure("bash not found - set its path in Settings")
    script = collector_path(config)
    if not script.exists():
        return record_failure(f"collector missing: {script.name}", f"no collector script at {script}")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    command = [bash, str(script), str(STATE_PATH), config["orgs"], str(config["max_age_days"])]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=COLLECTOR_TIMEOUT_SECONDS, check=False, **hidden_window_flags())
    except subprocess.TimeoutExpired:
        return record_failure(f"collector timed out after {COLLECTOR_TIMEOUT_SECONDS}s")
    except OSError as error:
        return record_failure(f"could not run the collector: {error.strerror or error}", str(error))
    if done.returncode != 0:
        # GitHub queries fail transiently, so the whole error is kept while the caller shows one line.
        return record_failure(describe_failure(done.stderr), done.stderr)
    try:
        digest = json.loads(done.stdout)
    except json.JSONDecodeError:
        return record_failure("collector returned unreadable output", done.stdout)
    if not isinstance(digest, dict):
        return record_failure("collector returned unreadable output", done.stdout)
    if "error" in digest:
        return record_failure(str(digest["error"]), done.stdout)
    return digest, ""
