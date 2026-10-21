"""Getting a pull request ready to review in an IDE, run against real git with the GitHub CLI stood in for."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gh_tray import local_review
from gh_tray.environment import shell_quote
from gh_tray.github import GitHubError
from gh_tray.local_review import PullRequest, ReviewError, open_for_review

PAGE = "https://github.com/acme/widget/pull/7"


def run_git(folder: Path, *arguments: str) -> None:
    """Run git in a folder, failing the test if git fails."""
    git = shutil.which("git")
    assert git, "these tests run real git"
    subprocess.run([git, "-C", str(folder), *arguments], check=True)


def make_clone(folder: Path, remote: str) -> Path:
    """Make a git repository with one commit and an origin remote, standing in for a clone."""
    folder.mkdir(parents=True)
    run_git(folder, "init", "-q")
    run_git(folder, "remote", "add", "origin", remote)
    run_git(
        folder, "-c", "user.name=tester", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "a"
    )
    return folder


class FakeGitHub:
    """Stands in for the GitHub CLI, recording each command and folder, and making clones and branches locally."""

    def __init__(self) -> None:
        """Start with nothing asked."""
        self.calls: list[tuple[str, Path | None]] = []

    def __call__(self, arguments: list[str], timeout: int = 0, folder: Path | None = None) -> str:
        """Record one command, making the clone a ``repo clone`` asks for or the branch a ``pr checkout`` names."""
        self.calls.append((" ".join(arguments[:2]), folder))
        if arguments[:2] == ["repo", "clone"]:
            make_clone(Path(arguments[3]), f"https://github.com/{arguments[2]}.git")
        if arguments[:2] == ["pr", "checkout"] and folder is not None:
            run_git(folder, "checkout", "-q", "-B", arguments[arguments.index("--branch") + 1])
        return ""


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Return the stand-in GitHub CLI the review code now calls."""
    fake = FakeGitHub()
    monkeypatch.setattr(local_review, "run", fake)
    return fake


def python_command(script: str) -> str:
    """Return an IDE command that runs a line of Python on the folder it is given."""
    return f"{shell_quote(sys.executable)} -c {shell_quote(script)} {{folder}}"


# Leaves a file named "opened" in the folder, so a test can see which folder the IDE was given.
RECORDING_IDE = python_command("import pathlib, sys; pathlib.Path(sys.argv[1], 'opened').touch()")


def test_a_pull_request_page_is_read_and_any_other_page_is_not():
    assert local_review.pull_request_at(PAGE) == PullRequest(PAGE, "acme", "widget", 7)
    assert local_review.pull_request_at("https://github.com/acme/widget/issues/7") is None


def test_a_clone_is_found_by_any_of_its_remotes_in_any_address_form_and_any_case(tmp_path):
    for name, remote in (("fork", "git@github.com:me/widget.git"), ("upstream", "ssh://git@github.com/Acme/Widget")):
        (tmp_path / name / ".git").mkdir(parents=True)
        (tmp_path / name / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {remote}\n', encoding="utf-8")
    (tmp_path / "notes").mkdir()
    assert local_review.find_clone(tmp_path, "acme/widget") == tmp_path / "upstream"
    assert local_review.find_clone(tmp_path, "acme/gadget") is None


# Opens nothing and leaves the worktree clean, so it can be removed.
QUIET_IDE = python_command("pass")


def review(page: str, ide: str, checkout_folder: Path) -> list[str]:
    """Open a pull request for review through the real flow, returning the steps it reported."""
    stages: list[str] = []
    open_for_review(page, ide, str(checkout_folder), stages.append)
    return stages


def test_review_adds_a_worktree_to_a_clone_in_the_checkout_folder_and_a_second_review_only_updates_it(tmp_path, github):
    make_clone(tmp_path / "widget", "git@github.com:Acme/Widget.git")
    worktree = tmp_path / "widget-pr7"
    review(PAGE, RECORDING_IDE, tmp_path)
    assert (worktree / "opened").exists()
    review(PAGE, RECORDING_IDE, tmp_path)
    assert github.calls == [("pr checkout", worktree), ("pr checkout", worktree)]


def test_removing_a_worktree_keeps_the_clone_made_in_the_checkout_folder_for_the_next_review(tmp_path, github):
    assert review(PAGE, QUIET_IDE, tmp_path) == [
        "Cloning acme/widget",
        "Adding the worktree widget-pr7",
        "Checking out pull request #7",
        "Opening the IDE",
    ]
    worktree = tmp_path / "widget-pr7"
    assert local_review.existing_worktree(PAGE, str(tmp_path)) == worktree
    local_review.remove_worktree(worktree)
    assert not worktree.exists()
    assert local_review.existing_worktree(PAGE, str(tmp_path)) is None
    review(PAGE, QUIET_IDE, tmp_path)
    assert [command for command, _folder in github.calls] == ["repo clone", "pr checkout", "pr checkout"]


def test_a_worktree_already_on_the_pull_requests_branch_is_reused_wherever_it_is(tmp_path, github):
    clone = make_clone(tmp_path / "widget", "https://github.com/acme/widget")
    elsewhere = tmp_path / "elsewhere" / "widget-pr7"
    run_git(clone, "worktree", "add", "-q", "-b", "widget-pr7", str(elsewhere))
    review(PAGE, QUIET_IDE, tmp_path)
    assert github.calls == [("pr checkout", elsewhere)]
    assert not (tmp_path / "widget-pr7").exists()


def test_the_settings_folder_a_jetbrains_ide_makes_does_not_stop_a_worktrees_removal(tmp_path, github):
    jetbrains = python_command(
        "import pathlib, sys; pathlib.Path(sys.argv[1], '.idea').mkdir(); "
        "pathlib.Path(sys.argv[1], '.idea', 'workspace.xml').touch()"
    )
    review(PAGE, jetbrains, tmp_path)
    worktree = tmp_path / "widget-pr7"
    assert (worktree / ".idea" / "workspace.xml").exists()
    local_review.remove_worktree(worktree)
    assert not worktree.exists()


def test_only_worktrees_on_their_own_review_branch_count_as_review_worktrees(tmp_path, github):
    review(PAGE, QUIET_IDE, tmp_path)
    review(PAGE.replace("/7", "/8"), QUIET_IDE, tmp_path)
    run_git(tmp_path / "widget", "worktree", "add", "-q", "-b", "someone-elses", str(tmp_path / "widget-pr9"))
    (tmp_path / "notes-pr3").mkdir()
    assert local_review.review_worktrees(str(tmp_path)) == [tmp_path / "widget-pr7", tmp_path / "widget-pr8"]


def test_a_worktree_holding_uncommitted_work_is_not_removed(tmp_path, github):
    review(PAGE, RECORDING_IDE, tmp_path)
    with pytest.raises(ReviewError, match="untracked"):
        local_review.remove_worktree(tmp_path / "widget-pr7")
    assert (tmp_path / "widget-pr7" / "opened").exists()


def make_leftover(folder: Path) -> None:
    """Leave what a removal that failed part way leaves of a worktree: its folder and a stray file, but no .git."""
    folder.mkdir()
    (folder / "stray.txt").write_text("left behind", encoding="utf-8")


def test_what_a_failed_removal_left_is_cleared_by_the_next_review_or_removal(tmp_path, github):
    make_clone(tmp_path / "widget", "https://github.com/acme/widget")
    make_leftover(tmp_path / "widget-pr7")
    assert review(PAGE, QUIET_IDE, tmp_path)[0] == "Clearing what an earlier removal left of widget-pr7"
    assert (tmp_path / "widget-pr7" / ".git").is_file()
    make_leftover(tmp_path / "widget-pr8")
    local_review.remove_worktree(tmp_path / "widget-pr8")
    assert not (tmp_path / "widget-pr8").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete a file another program has open")
def test_a_folder_held_open_is_reported_with_a_hint_to_close_the_ide(tmp_path):
    make_leftover(tmp_path / "widget-pr7")
    with (
        (tmp_path / "widget-pr7" / "stray.txt").open(encoding="utf-8"),
        pytest.raises(ReviewError, match="Close any IDE"),
    ):
        local_review.remove_worktree(tmp_path / "widget-pr7")


def test_a_clone_cut_short_is_cleared_away_so_the_next_review_clones_afresh(tmp_path, monkeypatch):
    def slow_clone(arguments: list[str], timeout: int = 0, folder: Path | None = None) -> str:
        (Path(arguments[3]) / ".git").mkdir(parents=True)
        raise GitHubError("GitHub did not answer") from subprocess.TimeoutExpired(arguments, timeout)

    monkeypatch.setattr(local_review, "run", slow_clone)
    with pytest.raises(ReviewError, match="took over"):
        review(PAGE, QUIET_IDE, tmp_path)
    assert not (tmp_path / "widget").exists()


def test_an_ide_command_that_fails_at_once_is_reported(tmp_path, github):
    make_clone(tmp_path / "widget", "https://github.com/acme/widget")
    with pytest.raises(ReviewError, match="exit code 3"):
        review(PAGE, python_command("raise SystemExit(3)"), tmp_path)


def test_an_ide_launcher_missing_from_the_path_is_named(tmp_path, github):
    make_clone(tmp_path / "widget", "https://github.com/acme/widget")
    with pytest.raises(ReviewError, match="no-such-ide is not on your PATH"):
        review(PAGE, "no-such-ide {folder}", tmp_path)


def test_review_needs_both_the_ide_command_and_the_checkout_folder(tmp_path):
    with pytest.raises(ReviewError, match="Settings"):
        open_for_review(PAGE, RECORDING_IDE, "", [].append)


def test_an_ide_command_without_the_placeholder_gets_the_folder_on_the_end(tmp_path):
    assert local_review.ide_command_line("pycharm64", tmp_path) == f"pycharm64 {shell_quote(str(tmp_path))}"
