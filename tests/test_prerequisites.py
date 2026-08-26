"""Deciding what outside tools are missing, and which of them may be installed without asking for a root shell."""

from __future__ import annotations

import pytest

from gh_tray import prerequisites


@pytest.fixture
def nothing_installed(monkeypatch):
    """Pretend no outside tool is present."""
    monkeypatch.setattr(prerequisites, "github_cli", lambda: None)
    monkeypatch.setattr(prerequisites, "gh_dash_installed", lambda: False)
    monkeypatch.setattr(prerequisites, "signed_in", lambda: False)


@pytest.fixture
def everything_installed(monkeypatch):
    """Pretend every outside tool is present and signed in."""
    monkeypatch.setattr(prerequisites, "github_cli", lambda: "/usr/bin/gh")
    monkeypatch.setattr(prerequisites, "gh_dash_installed", lambda: True)
    monkeypatch.setattr(prerequisites, "signed_in", lambda: True)


def test_nothing_is_missing_when_everything_is_present(everything_installed):
    assert prerequisites.missing() == []


def test_everything_is_missing_when_nothing_is_present(nothing_installed):
    assert [requirement.name for requirement in prerequisites.missing()] == ["GitHub CLI", "GitHub sign-in", "gh-dash"]


def test_each_requirement_says_why_it_is_needed(nothing_installed):
    assert all(requirement.summary for requirement in prerequisites.missing())


def test_signing_in_is_never_done_for_the_user(nothing_installed):
    # Signing in means entering credentials, which is the user's to do and nobody else's.
    sign_in = next(requirement for requirement in prerequisites.missing() if requirement.name == "GitHub sign-in")
    assert sign_in.installable is False
    assert "gh auth login" in sign_in.manual


def test_the_dashboard_cannot_be_installed_before_the_tool_that_installs_it(nothing_installed):
    dashboard = next(requirement for requirement in prerequisites.missing() if requirement.name == "gh-dash")
    assert dashboard.installable is False


def test_the_dashboard_can_be_installed_once_the_tool_is_there(monkeypatch):
    monkeypatch.setattr(prerequisites, "github_cli", lambda: "/usr/bin/gh")
    monkeypatch.setattr(prerequisites, "gh_dash_installed", lambda: False)
    monkeypatch.setattr(prerequisites, "signed_in", lambda: True)
    dashboard = next(requirement for requirement in prerequisites.missing() if requirement.name == "gh-dash")
    assert dashboard.command == ["gh", "extension", "install", "dlvhdr/gh-dash"]


def test_requirements_are_ordered_so_each_can_be_met_in_turn(nothing_installed):
    # The tool must arrive before it can sign in, and sign in before its extensions are worth having.
    first, *_rest = (requirement.name for requirement, _present in prerequisites.requirements())
    assert first == "GitHub CLI"


def test_windows_installs_the_tool_through_its_own_package_manager(monkeypatch):
    monkeypatch.setattr(prerequisites.sys, "platform", "win32")
    monkeypatch.setattr(prerequisites.shutil, "which", lambda name: "C:/winget.exe" if name == "winget" else None)
    name, arguments = prerequisites.package_manager()
    assert name == "winget"
    assert prerequisites.github_package() == "GitHub.cli"
    assert "--silent" in arguments


def test_macos_installs_the_tool_through_homebrew(monkeypatch):
    monkeypatch.setattr(prerequisites.sys, "platform", "darwin")
    monkeypatch.setattr(prerequisites.shutil, "which", lambda name: "/opt/brew" if name == "brew" else None)
    assert prerequisites.package_manager() == ("brew", ["brew", "install"])
    assert prerequisites.github_package() == "gh"


def test_a_platform_needing_a_root_shell_is_left_to_the_user(monkeypatch):
    # Running apt or dnf means acquiring root, which a desktop application has no business doing quietly.
    monkeypatch.setattr(prerequisites.sys, "platform", "linux")
    monkeypatch.setattr(prerequisites.shutil, "which", lambda _name: "/usr/bin/apt")
    assert prerequisites.package_manager() is None


def test_a_windows_without_its_package_manager_is_left_to_the_user(monkeypatch):
    monkeypatch.setattr(prerequisites.sys, "platform", "win32")
    monkeypatch.setattr(prerequisites.shutil, "which", lambda _name: None)
    assert prerequisites.package_manager() is None


def test_anything_that_cannot_be_installed_says_how_to_install_it(nothing_installed, monkeypatch):
    monkeypatch.setattr(prerequisites, "package_manager", lambda: None)
    assert all(requirement.manual for requirement in prerequisites.missing() if not requirement.installable)


def test_nothing_is_run_for_a_requirement_that_cannot_be_installed(monkeypatch):
    ran = []
    monkeypatch.setattr(prerequisites.subprocess, "run", lambda *arguments, **keywords: ran.append(arguments))
    assert prerequisites.install(prerequisites.Requirement("a thing", "why", [], manual="do it yourself")) is False
    assert ran == []


def test_a_failed_install_is_reported_rather_than_assumed_to_have_worked(monkeypatch):
    class Failed:
        returncode = 1

    monkeypatch.setattr(prerequisites.subprocess, "run", lambda *arguments, **keywords: Failed())
    assert prerequisites.install(prerequisites.Requirement("a thing", "why", ["install", "thing"])) is False


def test_an_install_that_cannot_even_start_is_reported(monkeypatch):
    def refuse(*_arguments, **_keywords):
        raise OSError("no such program")

    monkeypatch.setattr(prerequisites.subprocess, "run", refuse)
    assert prerequisites.install(prerequisites.Requirement("a thing", "why", ["install", "thing"])) is False
