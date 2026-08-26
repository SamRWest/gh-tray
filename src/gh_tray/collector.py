"""Gathering everything the application needs from GitHub, in one pass.

Collecting is deliberately separate from deciding what changed: this module only reports what is true now, and
knows nothing about what was true last time.

The pull request search asks for the last commit's author, the most recent review's author and the most recent
comment's author alongside the state of each pull request, because those are what name the person behind a change.
The notifications feed identifies a mention only by the comment it points at, so the first few of those are looked
up one by one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger

from .config import ERROR_LOG_PATH, STATE_PATH
from .github import GitHubError, api, search_pull_requests, viewer
from .storage import read_json, write_json_atomic, write_text_atomic

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FIRST_RUN_WINDOW = timedelta(days=1)
# One request each, so only the first few mentions are traced back to whoever wrote them.
MENTION_LOOKUP_LIMIT = 10

AUTHORED = "is:pr is:open author:@me archived:false"
REVIEWING = "is:pr is:open review-requested:@me archived:false"

SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 40, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url isDraft createdAt updatedAt totalCommentsCount mergeable reviewDecision
        repository { nameWithOwner }
        author { login }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } author { user { login } } } } }
        latestReviews(last: 1) { nodes { author { login } } }
        comments(last: 1) { nodes { author { login } } }
      }
    }
  }
}
"""


def now_stamp() -> str:
    """Return the current time as GitHub writes timestamps."""
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


def nested(node: dict, *path: str) -> str:
    """Follow a chain of keys through a reply, returning an empty string the moment one is missing.

    GitHub omits rather than nulls a great deal, and a login can go missing at any level: a deleted account, a
    commit by someone with no GitHub user, a pull request with no reviews yet.

    :param node: the object to walk
    :param path: the keys to follow, where a list is entered at its first element
    :return: the value found, as text, or an empty string
    """
    current: object = node
    for key in path:
        if isinstance(current, list):
            current = current[0] if current else None
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    if isinstance(current, list):
        current = current[0] if current else None
    return "" if current is None else str(current)


def normalise(node: dict, side: str) -> dict:
    """Turn one pull request from GitHub's shape into the flat record the rest of the application reads.

    :param node: a pull request as GitHub returned it
    :param side: ``authored`` or ``reviewing``
    """
    repo = nested(node, "repository", "nameWithOwner")
    number = node.get("number")
    return {
        "key": f"{repo}#{number}",
        "side": side,
        "repo": repo,
        "number": number,
        "title": node.get("title") or "",
        "url": node.get("url") or "",
        "isDraft": bool(node.get("isDraft")),
        "createdAt": node.get("createdAt") or "",
        "updatedAt": node.get("updatedAt") or "",
        "author": nested(node, "author", "login") or "unknown",
        "comments": node.get("totalCommentsCount") or 0,
        "reviewDecision": node.get("reviewDecision") or "NONE",
        "mergeable": node.get("mergeable") or "UNKNOWN",
        "ci": nested(node, "commits", "nodes", "commit", "statusCheckRollup", "state") or "NO_CHECKS",
        "lastCommitBy": nested(node, "commits", "nodes", "commit", "author", "user", "login"),
        "lastReviewBy": nested(node, "latestReviews", "nodes", "author", "login"),
        "lastCommentBy": nested(node, "comments", "nodes", "author", "login"),
    }


def drop_stale(pull_requests: list[dict], max_age_days: int, now: datetime) -> tuple[list[dict], int]:
    """Remove pull requests nobody has touched for a long time.

    The last-updated time is what is tested, never the creation date. A pull request is never updated before it is
    created, so an update cutoff already excludes everything older; testing creation as well would discard old
    branches that are still being worked on.

    :param pull_requests: the records to filter
    :param max_age_days: how old is too old, or zero to keep everything
    :param now: the moment to measure against
    :return: the records worth showing, and how many were dropped
    """
    if not max_age_days:
        return pull_requests, 0
    cutoff = (now - timedelta(days=max_age_days)).strftime(TIMESTAMP_FORMAT)
    kept = [pull_request for pull_request in pull_requests if pull_request["updatedAt"] >= cutoff]
    return kept, len(pull_requests) - len(kept)


def page_url(api_url: str) -> str:
    """Turn an interface address for a pull request into the page a person can open.

    :param api_url: an address such as ``https://api.github.com/repos/owner/name/pulls/7``
    """
    return api_url.replace("api.github.com/repos", "github.com").replace("/pulls/", "/pull/")


def collect_mentions(since: str) -> list[dict]:
    """Return the mentions raised since a moment, each named with whoever wrote it where that can be found.

    :param since: the earliest moment to report, as a GitHub timestamp
    """
    feed = api(f"notifications?all=false&since={since}&per_page=100")
    if not isinstance(feed, list):
        return []
    mentions = []
    looked_up = 0
    for notification in feed:
        if notification.get("reason") not in ("mention", "team_mention"):
            continue
        subject = notification.get("subject") or {}
        actor = ""
        comment_url = subject.get("latest_comment_url") or ""
        if comment_url and looked_up < MENTION_LOOKUP_LIMIT:
            looked_up += 1
            try:
                comment = api(comment_url.replace("https://api.github.com/", ""))
                actor = nested(comment, "user", "login") if isinstance(comment, dict) else ""
            except GitHubError as error:
                logger.debug("could not find out who wrote a mention: {}", error)
        mentions.append(
            {
                "repo": nested(notification, "repository", "full_name"),
                "title": subject.get("title") or "",
                "type": subject.get("type") or "",
                "reason": notification.get("reason") or "mention",
                "updatedAt": notification.get("updated_at") or "",
                "url": page_url(subject.get("url") or ""),
                "actor": actor,
            }
        )
    return mentions


def read_last_run() -> str:
    """Return when the last collection ran, or an empty string if none has."""
    stored, _damaged = read_json(STATE_PATH)
    return stored.get("lastRunAt", "") if isinstance(stored, dict) else ""


def collect(config: dict) -> tuple[dict | None, str]:
    """Gather everything the application needs from GitHub.

    :param config: current settings, supplying the age cutoff
    :return: the digest and an empty string on success, or None and a description of the failure
    """
    started = datetime.now(UTC)
    since = read_last_run() or (started - FIRST_RUN_WINDOW).strftime(TIMESTAMP_FORMAT)
    try:
        signed_in_as = viewer()
        authored = [normalise(node, "authored") for node in search_pull_requests(SEARCH_QUERY, AUTHORED)]
        reviewing = [normalise(node, "reviewing") for node in search_pull_requests(SEARCH_QUERY, REVIEWING)]
        mentions = collect_mentions(since)
    except GitHubError as error:
        write_text_atomic(ERROR_LOG_PATH, f"{started.isoformat()}\n{error}\n")
        logger.error("collection failed: {}", error)
        return None, str(error)[:120]

    cutoff = config.get("max_age_days", 0)
    authored, hidden_authored = drop_stale(authored, cutoff, started)
    reviewing, hidden_reviewing = drop_stale(reviewing, cutoff, started)
    write_json_atomic(STATE_PATH, {"lastRunAt": started.strftime(TIMESTAMP_FORMAT)})
    logger.info(
        "collected {} authored and {} awaiting review, {} mention(s), {} hidden as stale",
        len(authored),
        len(reviewing),
        len(mentions),
        hidden_authored + hidden_reviewing,
    )
    return {
        "window": {"since": since, "until": started.strftime(TIMESTAMP_FORMAT)},
        "staleFilter": {"maxAgeDays": cutoff, "hiddenAuthored": hidden_authored, "hiddenReviewing": hidden_reviewing},
        # Who is signed in, so that changes the user caused themselves can be told apart from ones done to them.
        "viewer": signed_in_as,
        "authored": authored,
        "reviewing": reviewing,
        "mentions": mentions,
    }, ""
