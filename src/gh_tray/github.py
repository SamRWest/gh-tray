"""Talking to GitHub through the signed-in command line tool.

The tool is used rather than the web interface directly so that this application never handles a token: it borrows
whatever the user has already signed in with, and stops working the moment they sign out, which is what anyone
would expect.

Every call goes out and comes back as JSON. Failures are raised as one exception type carrying a description short
enough to put in front of a user, since the caller turns them into a line of hover text.
"""

from __future__ import annotations

import json
import subprocess
import time

from loguru import logger

from .environment import github_cli, run_quietly

CALL_TIMEOUT_SECONDS = 60
# GitHub answers a heavy search with an error often enough that retrying is normal rather than exceptional.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 3
# A larger page provokes errors, and five pages is far more than a person can have open.
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
    """Return whether a failure is one a retry cannot fix.

    GitHub names the status it answered with. Anything in the client-error range means the request itself is the
    problem - the thing is deleted, private, or never existed - and asking again gets the same answer. The one
    exception is the too-many-requests status, which is exactly what retrying with a pause is for.

    :param description: the failure as :func:`first_error_line` reported it
    """
    return "HTTP 4" in description and "HTTP 429" not in description


def api(path: str) -> object:
    """Fetch one REST path, retrying while GitHub is unhappy.

    Retried for the same reason the query path is: GitHub fails transiently often enough that one failure says
    nothing, and a lookup that silently comes back empty loses a name for good. A failure naming a client error is
    raised at once instead, since a deleted comment stays deleted however many times it is asked for.

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
    """Return the login of the signed-in account.

    Asked afresh each poll rather than remembered, since somebody can sign in as a different account at any time.

    :return: the login, or an empty string when it cannot be read
    """
    found = api("user")
    return str(found.get("login", "")) if isinstance(found, dict) else ""


def graphql(query: str, variables: dict[str, str]) -> dict:
    """Run one GraphQL query, retrying while GitHub is unhappy.

    An error from GitHub arrives as well-formed JSON carrying no data, so a reply counts as usable only once it
    actually holds results. Accepting one that does not would abandon the whole collection rather than retry it.

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
    """Return the logins of the organisations the signed-in account belongs to, in alphabetical order.

    Membership is all this can see. An account can have a hand in pull requests elsewhere, as an outside
    collaborator, which is why the settings turn organisations off rather than on.

    :return: the logins, or an empty list when the account belongs to none
    :raises GitHubError: when GitHub cannot be asked
    """
    found = api("user/orgs?per_page=100")
    if not isinstance(found, list):
        return []
    logins = [str(item.get("login", "")) for item in found if isinstance(item, dict) and item.get("login")]
    return sorted(logins, key=str.casefold)
