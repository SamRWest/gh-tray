"""Gets a pull request ready to review in an IDE: a worktree of a local clone, on a branch tracking the pull request.

No IDE has a command that opens one pull request. JetBrains IDEs and VS Code's GitHub Pull Requests extension instead
look up the pull request whose head the checked-out branch tracks, and start their review mode on it, so the IDE is
only told which folder to open.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from gh_tray.environment import FOLDER_PLACEHOLDER, no_console_flag, run_quietly, shell_quote
from gh_tray.github import first_error_line, run

# Review worktrees sit together under the code folder, named for what they hold, so they are easy to clear away.
WORKTREE_FOLDER = "temp"
GIT_TIMEOUT_SECONDS = 60
CHECKOUT_TIMEOUT_SECONDS = 120
# A first clone of a large repository can take minutes.
CLONE_TIMEOUT_SECONDS = 900
# Long enough for a shell to report a launcher it cannot find, short enough not to wait on one that stays running.
LAUNCH_CHECK_SECONDS = 5

PULL_REQUEST_URL = re.compile(r"^https?://[^/]+/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/(?P<number>\d+)/?$")
REMOTE_URL_LINE = re.compile(r"^\s*url\s*=\s*(\S+)", re.MULTILINE)
# The owner and name that end every form of remote address: https://, ssh:// and git@host:owner/name alike.
REMOTE_REPOSITORY = re.compile(r"[/:](?P<repository>[^/:]+/[^/]+?)(?:\.git)?/?$")


class ReviewError(RuntimeError):
    """A pull request could not be got ready to review, carrying a description fit to show a user."""


@dataclass(frozen=True)
class PullRequest:
    """One pull request, as its page on GitHub names it."""

    url: str
    owner: str
    name: str
    number: int

    @property
    def repository(self) -> str:
        """Return the repository as ``owner/name``."""
        return f"{self.owner}/{self.name}"


def pull_request_at(url: str) -> PullRequest | None:
    """Return the pull request a page shows, or None when the page is not a pull request.

    :param url: the page, such as ``https://github.com/acme/widget/pull/7``
    """
    found = PULL_REQUEST_URL.match(url)
    return PullRequest(url, found["owner"], found["name"], int(found["number"])) if found else None


def remote_repositories(clone: Path) -> set[str]:
    """Return every repository a clone's remotes point at, as lower-case ``owner/name``.

    Read from the clone's own settings file rather than by running git, so scanning a folder of clones stays quick.
    :param clone: the clone's top folder; anything else has no remotes
    """
    try:
        settings = (clone / ".git" / "config").read_text(encoding="utf-8")
    except OSError:
        return set()
    found = (REMOTE_REPOSITORY.search(url) for url in REMOTE_URL_LINE.findall(settings))
    return {match["repository"].casefold() for match in found if match}


def find_clone(code_folder: Path, repository: str) -> Path | None:
    """Return a clone directly inside the code folder with a remote for a repository, or None when there is none.

    :param code_folder: the folder holding clones
    :param repository: the repository as ``owner/name``
    """
    wanted = repository.casefold()
    return next((child for child in sorted(code_folder.iterdir()) if wanted in remote_repositories(child)), None)


def worktree_folder(code_folder: Path, pull: PullRequest) -> Path:
    """Return where a pull request's review worktree lives, such as ``<code folder>/temp/widget-pr7``.

    :param code_folder: the folder holding clones
    :param pull: the pull request
    """
    return code_folder / WORKTREE_FOLDER / f"{pull.name}-pr{pull.number}"


def git(arguments: list[str], clone: Path) -> None:
    """Run git on a clone.

    :param arguments: what to pass git, without git itself
    :param clone: the clone to act on
    :raises ReviewError: when git is missing, fails, or takes too long
    """
    tool = shutil.which("git")
    if not tool:
        raise ReviewError("git not found - install it")
    try:
        done = run_quietly([tool, "-C", str(clone), *arguments], timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as expiry:
        raise ReviewError(f"git did not finish within {GIT_TIMEOUT_SECONDS}s") from expiry
    if done.returncode != 0:
        raise ReviewError(first_error_line(done.stderr))


def clone_into(code_folder: Path, repository: str) -> Path:
    """Clone a repository into the code folder, in a folder named after it, and return the clone.

    :param code_folder: the folder holding clones
    :param repository: the repository as ``owner/name``
    :raises GitHubError: when the clone fails, for instance because a folder of that name is already there
    """
    clone = code_folder / repository.split("/")[1]
    logger.info("cloning {} into {}", repository, clone)
    run(["repo", "clone", repository, str(clone)], timeout=CLONE_TIMEOUT_SECONDS)
    return clone


def add_worktree(clone: Path, folder: Path) -> None:
    """Add a worktree to a clone, detached until a pull request's branch is checked out in it.

    Pruning first forgets worktrees whose folders were deleted by hand, which would otherwise keep their branches.
    :param clone: the clone to add the worktree to
    :param folder: where the worktree goes
    """
    git(["worktree", "prune"], clone)
    git(["worktree", "add", "--detach", str(folder)], clone)


def check_out(pull: PullRequest, folder: Path) -> None:
    """Check a pull request's branch out in a worktree, or bring it up to date, tracking the pull request's head.

    :param pull: the pull request
    :param folder: the worktree
    :raises GitHubError: when the checkout fails, for instance over uncommitted changes in the way
    """
    run(["pr", "checkout", pull.url, "--branch", f"pr-{pull.number}"], timeout=CHECKOUT_TIMEOUT_SECONDS, folder=folder)


def ide_command_line(command: str, folder: Path) -> str:
    """Return the command line that opens a folder in the IDE, the folder put in place of ``{folder}`` or at the end.

    :param command: the IDE command from the settings
    :param folder: the folder to open
    """
    quoted = shell_quote(str(folder))
    return command.replace(FOLDER_PLACEHOLDER, quoted) if FOLDER_PLACEHOLDER in command else f"{command} {quoted}"


def open_in_ide(command: str, folder: Path) -> None:
    """Open a folder in the IDE, waiting briefly so a launcher that fails at once is reported.

    :param command: the IDE command from the settings
    :param folder: the folder to open
    :raises ReviewError: when the command ends at once with a failure
    """
    line = ide_command_line(command, folder)
    quiet = subprocess.DEVNULL
    # A user-typed command line needs a shell for its quoting and arguments, and for launchers that are scripts.
    launched = subprocess.Popen(  # noqa: S602
        line, shell=True, stdin=quiet, stdout=quiet, stderr=quiet, creationflags=no_console_flag()
    )
    try:
        code = launched.wait(LAUNCH_CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        return
    if code != 0:
        raise ReviewError(f"the IDE command failed with exit code {code}: {line}")


def open_for_review(url: str, code_folder: str, ide_command: str) -> None:
    """Open a pull request in the IDE, finding or cloning its repository and adding or updating its worktree.

    Slow, since a first clone can take minutes, so meant for a thread other than the toolkit's.
    :param url: the pull request's page
    :param code_folder: the folder holding clones, from the settings
    :param ide_command: the command that opens a folder in the IDE, from the settings
    :raises ReviewError: when the page or settings will not do, or git fails
    :raises GitHubError: when cloning or checking out fails
    """
    pull = pull_request_at(url)
    if pull is None:
        raise ReviewError(f"{url} is not a pull request")
    if not code_folder or not ide_command:
        raise ReviewError("set the IDE command and code folder in Settings first")
    root = Path(code_folder).expanduser()
    if not root.is_dir():
        raise ReviewError(f"the code folder {root} does not exist")
    folder = worktree_folder(root, pull)
    if not folder.exists():
        add_worktree(find_clone(root, pull.repository) or clone_into(root, pull.repository), folder)
    check_out(pull, folder)
    logger.info("opening {} for review in {}", pull.url, folder)
    open_in_ide(ide_command, folder)
