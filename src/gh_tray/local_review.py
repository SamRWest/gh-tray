"""Gets a pull request ready to review in an IDE: a worktree of a local clone, on a branch tracking the pull request.

No IDE has a command that opens one pull request. VS Code's GitHub Pull Requests extension and the JetBrains GitHub
plugin instead look up the pull request whose head the checked-out branch tracks, so the IDE is only told which folder
to open. The JetBrains plugin only does so once its Pull Requests tool window has been opened in that project.
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
    A worktree's ``.git`` is a file, not a folder, so worktrees are passed over.
    :param clone: the clone's top folder; anything else has no remotes
    """
    try:
        settings = (clone / ".git" / "config").read_text(encoding="utf-8")
    except OSError:
        return set()
    found = (REMOTE_REPOSITORY.search(url) for url in REMOTE_URL_LINE.findall(settings))
    return {match["repository"].casefold() for match in found if match}


def find_clone(folder: Path, repository: str) -> Path | None:
    """Return a clone directly inside a folder with a remote for a repository, or None when there is none.

    :param folder: the folder holding clones
    :param repository: the repository as ``owner/name``
    """
    wanted = repository.casefold()
    return next((child for child in sorted(folder.iterdir()) if wanted in remote_repositories(child)), None)


def worktree_folder(checkout_folder: Path, pull: PullRequest) -> Path:
    """Return where a pull request's review worktree lives, such as ``<checkout folder>/widget-pr7``.

    :param checkout_folder: the folder review worktrees go in
    :param pull: the pull request
    """
    return checkout_folder / f"{pull.name}-pr{pull.number}"


def existing_worktree(url: str, checkout_folder: str) -> Path | None:
    """Return the review worktree of the pull request a page shows, or None when there is none.

    :param url: the pull request's page
    :param checkout_folder: the folder review worktrees go in, from the settings
    """
    pull = pull_request_at(url)
    if pull is None or not checkout_folder:
        return None
    folder = worktree_folder(Path(checkout_folder).expanduser(), pull)
    return folder if folder.is_dir() else None


def git(arguments: list[str], folder: Path) -> str:
    """Run git in a clone or worktree and return what it printed.

    :param arguments: what to pass git, without git itself
    :param folder: the clone or worktree to act on
    :raises ReviewError: when git is missing, fails, or takes too long
    """
    tool = shutil.which("git")
    if not tool:
        raise ReviewError("git not found - install it")
    try:
        done = run_quietly([tool, "-C", str(folder), *arguments], timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as expiry:
        raise ReviewError(f"git did not finish within {GIT_TIMEOUT_SECONDS}s") from expiry
    if done.returncode != 0:
        # Kept whole: git names the folder first, so cutting the line short can cut off the reason.
        raise ReviewError(first_error_line(done.stderr, limit=None))
    return done.stdout


def clone_into(folder: Path, repository: str) -> Path:
    """Clone a repository into a folder, in a folder named after it, and return the clone.

    :param folder: the folder to clone into
    :param repository: the repository as ``owner/name``
    :raises GitHubError: when the clone fails, for instance because a folder of that name is already there
    """
    clone = folder / repository.split("/")[1]
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


def remove_worktree(folder: Path) -> None:
    """Remove a review worktree, keeping its branch in the clone; git refuses one with uncommitted changes.

    Git runs from the clone, since Windows cannot delete the folder a process is working in.
    :param folder: the worktree
    :raises ReviewError: when git fails, for instance over uncommitted changes
    """
    shared = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], folder).strip()
    git(["worktree", "remove", str(folder)], Path(shared).parent)


def check_out(pull: PullRequest, folder: Path) -> None:
    """Check a pull request's branch out in a worktree, or bring it up to date, tracking the pull request's head.

    :param pull: the pull request
    :param folder: the worktree
    :raises GitHubError: when the checkout fails, for instance over uncommitted changes in the way
    """
    run(["pr", "checkout", pull.url, "--branch", f"pr-{pull.number}"], timeout=CHECKOUT_TIMEOUT_SECONDS, folder=folder)


def ide_command_line(command: str, folder: Path) -> str:
    """Return the command line that opens a folder in the IDE, the folder put in place of ``{folder}`` or at the end.

    :param command: the IDE command
    :param folder: the folder to open
    """
    quoted = shell_quote(str(folder))
    return command.replace(FOLDER_PLACEHOLDER, quoted) if FOLDER_PLACEHOLDER in command else f"{command} {quoted}"


def open_in_ide(command: str, folder: Path) -> None:
    """Open a folder in the IDE, waiting briefly so a launcher that fails at once is reported.

    :param command: the IDE command
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


def open_for_review(url: str, ide_command: str, checkout_folder: str, code_folder: str = "") -> None:
    """Open a pull request in the IDE, finding or cloning its repository and adding or updating its worktree.

    A clone is looked for in the code folder, then the checkout folder, and made in the first of them that is set.
    Slow, since a first clone can take minutes, so meant for a thread other than the toolkit's.
    :param url: the pull request's page
    :param ide_command: the command that opens a folder in the IDE
    :param checkout_folder: the folder review worktrees go in, from the settings
    :param code_folder: the folder holding the user's own clones, from the settings, or empty for none
    :raises ReviewError: when the page or settings will not do, or git fails
    :raises GitHubError: when cloning or checking out fails
    """
    pull = pull_request_at(url)
    if pull is None:
        raise ReviewError(f"{url} is not a pull request")
    if not ide_command or not checkout_folder:
        raise ReviewError("set the IDE command and checkout folder in Settings first")
    roots = [Path(folder).expanduser() for folder in (code_folder, checkout_folder) if folder]
    if missing := [root for root in roots if not root.is_dir()]:
        raise ReviewError(f"the folder {missing[0]} does not exist")
    folder = worktree_folder(roots[-1], pull)
    if not folder.exists():
        clone = next(filter(None, (find_clone(root, pull.repository) for root in roots)), None)
        add_worktree(clone or clone_into(roots[0], pull.repository), folder)
    check_out(pull, folder)
    logger.info("opening {} for review in {}", pull.url, folder)
    open_in_ide(ide_command, folder)
