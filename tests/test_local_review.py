"""Getting a pull request ready to review in an IDE, run against real git with the GitHub CLI stood in for."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gh_tray import local_review
from gh_tray.environment import shell_quote
from gh_tray.local_review import PullRequest, ReviewError, open_for_review

PAGE = "https://github.com/acme/widget/pull/7"


def make_clone(folder: Path, remote: str) -> Path:
    """Make a git repository with one commit and an origin remote, standing in for a clone."""
    git = shutil.which("git")
    assert git, "these tests run real git"
    folder.mkdir(parents=True)
    identity = ["-c", "user.name=tester", "-c", "user.email=tester@example.test"]
    for arguments in (
        ["init", "-q"],
        ["remote", "add", "origin", remote],
        [*identity, "commit", "-q", "--allow-empty", "-m", "start"],
    ):
        subprocess.run([git, "-C", str(folder), *arguments], check=True)
    return folder


class FakeGitHub:
    """Stands in for the GitHub CLI, recording each command and folder; a clone it is asked for is made locally."""

    def __init__(self) -> None:
        """Start with nothing asked."""
        self.calls: list[tuple[str, Path | None]] = []

    def __call__(self, arguments: list[str], timeout: int = 0, folder: Path | None = None) -> str:
        """Record one command, making the clone a ``repo clone`` asks for."""
        self.calls.append((" ".join(arguments[:2]), folder))
        if arguments[:2] == ["repo", "clone"]:
            make_clone(Path(arguments[3]), f"https://github.com/{arguments[2]}.git")
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


def test_review_adds_a_worktree_to_the_local_clone_and_a_second_review_only_updates_it(tmp_path, github):
    make_clone(tmp_path / "widget", "git@github.com:Acme/Widget.git")
    worktree = tmp_path / "temp" / "widget-pr7"
    open_for_review(PAGE, str(tmp_path), RECORDING_IDE)
    assert (worktree / "opened").exists()
    open_for_review(PAGE, str(tmp_path), RECORDING_IDE)
    assert github.calls == [("pr checkout", worktree), ("pr checkout", worktree)]


def test_review_clones_a_repository_the_code_folder_lacks(tmp_path, github):
    open_for_review(PAGE, str(tmp_path), RECORDING_IDE)
    assert [command for command, _folder in github.calls] == ["repo clone", "pr checkout"]
    assert (tmp_path / "temp" / "widget-pr7" / "opened").exists()


def test_an_ide_command_that_fails_at_once_is_reported(tmp_path, github):
    make_clone(tmp_path / "widget", "https://github.com/acme/widget")
    with pytest.raises(ReviewError, match="exit code 3"):
        open_for_review(PAGE, str(tmp_path), python_command("raise SystemExit(3)"))


def test_review_needs_both_the_ide_command_and_the_code_folder(tmp_path):
    with pytest.raises(ReviewError, match="Settings"):
        open_for_review(PAGE, str(tmp_path), "")


def test_an_ide_command_without_the_placeholder_gets_the_folder_on_the_end(tmp_path):
    assert local_review.ide_command_line("pycharm64", tmp_path) == f"pycharm64 {shell_quote(str(tmp_path))}"
