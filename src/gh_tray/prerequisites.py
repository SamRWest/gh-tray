"""Checking for the outside tools this application needs, and installing the ones that can be installed safely.

Only installs that manage their own elevation are ever run: a package manager that prompts for administrator rights
itself, or a GitHub extension that lands in the user's own directory. Anything needing a root shell is printed for
the user to run, because a desktop application quietly acquiring root is not a thing anyone should have to trust.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from loguru import logger

from .environment import github_cli, run_quietly

INSTALL_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class Requirement:
    """One outside tool, how to tell whether it is here, and how to get it."""

    name: str
    summary: str
    command: list[str]
    manual: str = ""

    @property
    def installable(self) -> bool:
        """Return whether this can be installed without a root shell."""
        return bool(self.command)


def package_manager() -> tuple[str, list[str]] | None:
    """Return the platform's package manager and the arguments that install with it.

    Only managers that prompt for elevation themselves are offered. A manager needing ``sudo`` is deliberately not
    run, so on most Linux systems the instruction is printed instead.

    :return: the manager's name and the leading arguments of an install command, or None when there is none to use
    """
    if sys.platform == "win32" and shutil.which("winget"):
        return "winget", ["winget", "install", "--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements", "--id"]
    if sys.platform == "darwin" and shutil.which("brew"):
        return "brew", ["brew", "install"]
    return None


def github_package() -> str:
    """Return the package name for the GitHub command line tool under this platform's manager."""
    return "GitHub.cli" if sys.platform == "win32" else "gh"


def gh_dash_installed() -> bool:
    """Return whether the dashboard extension is installed."""
    tool = github_cli()
    if not tool:
        return False
    done = run_quietly([tool, "extension", "list"])
    return "dlvhdr/gh-dash" in done.stdout


def signed_in() -> bool:
    """Return whether the GitHub command line tool is signed in."""
    tool = github_cli()
    if not tool:
        return False
    return run_quietly([tool, "auth", "status"]).returncode == 0


def requirements() -> list[tuple[Requirement, bool]]:
    """Return every outside tool this application needs, each paired with whether it is already here.

    :return: requirements in the order they must be satisfied, since each later one needs the earlier ones
    """
    manager = package_manager()
    github_install = [*manager[1], github_package()] if manager else []
    return [
        (
            Requirement(
                "GitHub CLI",
                "reads your pull requests, and is how this application signs in",
                github_install,
                manual="see https://github.com/cli/cli#installation",
            ),
            bool(github_cli()),
        ),
        (
            Requirement("GitHub sign-in", "without it every call is refused", [], manual="run: gh auth login"),
            signed_in(),
        ),
        (
            Requirement(
                "gh-dash",
                "the terminal dashboard a double click opens",
                ["gh", "extension", "install", "dlvhdr/gh-dash"] if github_cli() else [],
                manual="run: gh extension install dlvhdr/gh-dash",
            ),
            gh_dash_installed(),
        ),
    ]


def missing() -> list[Requirement]:
    """Return the requirements that are not satisfied."""
    return [requirement for requirement, present in requirements() if not present]


def install(requirement: Requirement) -> bool:
    """Install one requirement, if it is one that can be installed without a root shell.

    :param requirement: what to install
    :return: whether it is now present
    """
    if not requirement.installable:
        return False
    logger.info("installing {}: {}", requirement.name, " ".join(requirement.command))
    try:
        done = subprocess.run(requirement.command, timeout=INSTALL_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.error("could not install {}: {}", requirement.name, error)
        return False
    if done.returncode != 0:
        logger.error("installing {} failed with exit code {}", requirement.name, done.returncode)
    return done.returncode == 0
