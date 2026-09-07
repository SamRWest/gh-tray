"""Talks to GitHub via the signed-in CLI; no token handling, and failures raise one error type sized for hover text."""

from __future__ import annotations

import json
import subprocess
import time

from loguru import logger

from gh_tray.environment import github_cli, run_quietly

# Retrying is normal, since GitHub errors on heavy searches; five pages is more than anyone has open at once.
CALL_TIMEOUT_SECONDS = 60
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 3
PAGE_SIZE = 40
MAX_PAGES = 5


class GitHubError(RuntimeError):
    """A call to GitHub failed, carrying a description fit to show a user."""


def run(arguments: list[str], timeout: int = CALL_TIMEOUT_SECONDS) -> str:
    """Run the GitHub command line tool and return what it printed.

    :param arguments: what to pass the tool, without the tool itself
    :param timeout: how long to wait before giving up
    :return: standard output
    :raises GitHubError: when the tool is missing, fails, or takes too long
    """
    tool = github_cli()
    if not tool:
        raise GitHubError("GitHub CLI (gh) not found - install it and sign in")
    started = time.monotonic()
    try:
        done = run_quietly([tool, *arguments], timeout=timeout)
    except subprocess.TimeoutExpired as expiry:
        raise GitHubError(f"GitHub did not answer within {timeout}s") from expiry
    except OSError as error:
        raise GitHubError(f"could not run the GitHub CLI: {error.strerror or error}") from error
    logger.debug(
        "gh {} answered {} in {:.1f} s with {} characters",
        " ".join(arguments[:2]),
        done.returncode,
        time.monotonic() - started,
        len(done.stdout),
    )
    if done.returncode != 0:
        raise GitHubError(first_error_line(done.stderr))
    return done.stdout


def first_error_line(stderr: str) -> str:
    """Pick the most informative line out of a failed call.

    :param stderr: everything the tool wrote to its error stream
    :return: a short single-line description
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "the GitHub CLI failed without saying why"
    return next((line for line in lines if "error" in line.lower()), lines[0])[:120]


def parse(payload: str, what: str) -> object:
    """Read JSON that GitHub returned.

    :param payload: the text to read
    :param what: what was being fetched, for the error message
    :raises GitHubError: when the text is not readable JSON
    """
    try:
        return json.loads(payload or "null")
    except json.JSONDecodeError as error:
        raise GitHubError(f"{what} came back unreadable") from error


def looks_permanent(description: str) -> bool:
    """Return whether a failure is one a retry cannot fix (a pause fixes too-many-requests, not other 4xx).

    :param description: the failure as :func:`first_error_line` reported it
    """
    return "HTTP 4" in description and "HTTP 429" not in description


def api(path: str) -> object:
    """Fetch one REST path, retrying while GitHub is unhappy (a client error, e.g. deleted, is raised at once).

    :param path: the path to fetch, such as ``notifications?all=false``
    :raises GitHubError: when every attempt fails, or the failure is one retrying cannot fix
    """
    last = "GitHub returned nothing usable"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return parse(run(["api", path]), path)
        except GitHubError as error:
            last = str(error)
            if looks_permanent(last):
                raise
        logger.warning("GitHub call failed ({}), attempt {} of {}", last, attempt, RETRY_ATTEMPTS)
        if attempt < RETRY_ATTEMPTS:
            time.sleep(attempt * RETRY_BACKOFF_SECONDS)
    raise GitHubError(last)


def viewer() -> str:
    """Return the login of the signed-in account, asked afresh each poll since it can change at any time.

    :return: the login, or an empty string when it cannot be read
    """
    found = api("user")
    return str(found.get("login", "")) if isinstance(found, dict) else ""


def graphql(query: str, variables: dict[str, str]) -> dict:
    """Run one GraphQL query, retrying while GitHub is unhappy (an error arrives as JSON with no data).

    :param query: the query text
    :param variables: values for the query's variables
    :return: the ``data`` object
    :raises GitHubError: when every attempt fails
    """
    arguments = ["api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        arguments += ["-f", f"{name}={value}"]
    last = "GitHub returned nothing usable"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            answer = parse(run(arguments), "the search")
        except GitHubError as error:
            last = str(error)
            answer = None
        if isinstance(answer, dict) and isinstance(answer.get("data"), dict):
            return answer["data"]
        if isinstance(answer, dict) and answer.get("errors"):
            last = str(answer["errors"][0].get("message", last))[:120]
        logger.warning("GitHub call failed ({}), attempt {} of {}", last, attempt, RETRY_ATTEMPTS)
        if attempt < RETRY_ATTEMPTS:
            time.sleep(attempt * RETRY_BACKOFF_SECONDS)
    raise GitHubError(last)


def search_pull_requests(query: str, search: str) -> list[dict]:
    """Run a paged pull request search and return every node it yields.

    :param query: the GraphQL query text, which must accept ``q`` and ``cursor``
    :param search: the GitHub search expression
    :return: the pull request nodes, in the order GitHub gave them
    """
    nodes: list[dict] = []
    cursor = ""
    for _page in range(MAX_PAGES):
        variables = {"q": search} | ({"cursor": cursor} if cursor else {})
        data = graphql(query, variables)
        found = data.get("search") or {}
        nodes += [node for node in found.get("nodes", []) if node.get("number") is not None]
        page = found.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor", "")
        if not cursor:
            break
    return nodes


def organisations() -> list[str]:
    """Return the signed-in account's organisations, alphabetically.

    Membership is all this API can see. An account can have a hand in pull requests elsewhere, as an outside
    collaborator, which is why the settings turn organisations off rather than on.

    :return: the logins, or an empty list when the account belongs to none
    :raises GitHubError: when GitHub cannot be asked
    """
    found = api("user/orgs?per_page=100")
    if not isinstance(found, list):
        return []
    logins = [str(item.get("login", "")) for item in found if isinstance(item, dict) and item.get("login")]
    return sorted(logins, key=str.casefold)
