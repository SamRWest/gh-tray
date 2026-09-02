"""Gathering everything the application needs from GitHub, in one pass.

Collecting is deliberately separate from deciding what changed: this module only reports what is true now, and
knows nothing about what was true last time.

The pull request search asks for the last commit's author, the most recent review's author and the most recent
comment's author alongside the state of each pull request, because those are what name the person behind a change.
The notifications feed identifies a mention only by the comment it points at, so the first few of those are looked
up one by one.

Recently closed pull requests are collected too, kept apart from the open ones: they raise no notifications, but a
row about one can then say it is merged or closed rather than guessing.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from loguru import logger

from .config import ERROR_LOG_PATH, STATE_PATH
from .github import GitHubError, api, search_pull_requests, viewer
from .storage import read_json, write_json_atomic, write_text_atomic

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FIRST_RUN_WINDOW = timedelta(days=1)
# One request each, so only the first few mentions are traced back to whoever wrote them.
MENTION_LOOKUP_LIMIT = 10
# How many askings go out at once. GitHub asks callers not to flood it, and four is enough to overlap the waiting
# without becoming a flood.
CONCURRENT_ASKINGS = 4
CONCURRENT_LOOKUPS = 4

AUTHORED = "is:pr is:open author:@me archived:false"
REVIEWING = "is:pr is:open review-requested:@me archived:false"
# Closed pull requests the user had a hand in, so a row about one can say it is finished rather than guessing.
# Asked for newest first, because the search stops after a few pages and GitHub's best-match order can drop a
# freshly closed pull request while keeping old history. Newest first, the cap only ever drops the oldest.
CLOSED = "is:pr is:closed involves:@me archived:false sort:updated-desc"
# How far back to look for closed pull requests, regardless of the configured age cutoff. Rows referencing one
# live in the event log, which is trimmed to a short tail, so anything older than this has left the window.
CLOSED_LOOKBACK_DAYS = 30


def search_for(base: str, max_age_days: int, now: datetime) -> str:
    """Add the age cutoff to a search, so GitHub leaves out what would only be thrown away on arrival.

    Without this the search returns everything ever opened and most of it is dropped here, which on a long-lived
    account means fetching two pages to keep one. Each page is a slow request, so this is most of a poll's time.

    :param base: the search expression
    :param max_age_days: how old is too old, or zero to keep everything
    :param now: the moment to measure against
    """
    if not max_age_days:
        return base
    return f"{base} updated:>{(now - timedelta(days=max_age_days)).strftime('%Y-%m-%d')}"


SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 40, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url state isDraft createdAt updatedAt totalCommentsCount mergeable reviewDecision
        repository { nameWithOwner }
        author { login }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } author { user { login } } } } }
        latestReviews(last: 1) { nodes { author { login } } }
        comments(last: 1) { nodes { author { login } createdAt } }
        reviews(last: 1) {
          nodes { author { login } comments(last: 1) { nodes { createdAt replyTo { author { login } } } } }
        }
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


def newest_comment_is_marginal(node: dict) -> bool:
    """Return whether a pull request's newest comment sits against the diff rather than in the conversation.

    The comment count GitHub reports covers both, but the list of comments it returns holds only the conversation.
    A pull request reviewed entirely in the margin therefore has a count that moves with nothing to show for it,
    so the margin has to be asked about separately: the newest comment of the last review, since a reply arrives
    wrapped in a fresh review of its own. A review submitted with no comments in the margin carries no time here
    and never wins.

    :param node: a pull request as GitHub returned it
    """
    said_at = nested(node, "comments", "nodes", "createdAt")
    margin_at = nested(node, "reviews", "nodes", "comments", "nodes", "createdAt")
    return margin_at > said_at


def last_commenter(node: dict) -> str:
    """Return whoever commented last on a pull request, whether in the conversation or against the diff.

    Without the diff's half, the window can neither say who commented nor tell the user's own comments from
    anyone else's.

    :param node: a pull request as GitHub returned it
    :return: a login, or an empty string when neither can be found
    """
    if newest_comment_is_marginal(node):
        return nested(node, "reviews", "nodes", "author", "login")
    return nested(node, "comments", "nodes", "author", "login")


def last_comment_answers(node: dict) -> str:
    """Return whose comment the newest comment answers, where it answers one at all.

    Only a comment against the diff can be an answer; conversation comments stand alone. This is what lets a
    comment on somebody else's pull request matter when it answers one of the user's own review comments.

    :param node: a pull request as GitHub returned it
    :return: the login answered, or an empty string where the newest comment answers nobody
    """
    if not newest_comment_is_marginal(node):
        return ""
    return nested(node, "reviews", "nodes", "comments", "nodes", "replyTo", "author", "login")


def normalise(node: dict, side: str) -> dict:
    """Turn one pull request from GitHub's shape into the flat record the rest of the application reads.

    :param node: a pull request as GitHub returned it
    :param side: ``authored``, ``reviewing`` or ``closed``
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
        "state": node.get("state") or "OPEN",
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
        "lastCommentBy": last_commenter(node),
        "lastCommentAnswers": last_comment_answers(node),
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


def comment_author(comment_url: str) -> str:
    """Return who wrote one comment, or nothing if it cannot be read.

    :param comment_url: the comment's address on GitHub's interface
    """
    if not comment_url:
        return ""
    try:
        comment = api(comment_url.replace("https://api.github.com/", ""))
    except GitHubError as error:
        logger.warning("could not find out who wrote a mention: {}", error)
        return ""
    return nested(comment, "user", "login") if isinstance(comment, dict) else ""


def thread_author(subject_url: str) -> str:
    """Return whose thread a notification is about, or nothing if it cannot be read.

    :param subject_url: the thread's address on GitHub's interface
    """
    if not subject_url:
        return ""
    try:
        thread = api(subject_url.replace("https://api.github.com/", ""))
    except GitHubError as error:
        logger.warning("could not find out whose thread a mention is on: {}", error)
        return ""
    return nested(thread, "user", "login") if isinstance(thread, dict) else ""


def collect_mentions(since: str) -> list[dict]:
    """Return the mentions raised since a moment, each named with whoever wrote it where that can be found.

    :param since: the earliest moment to report, as a GitHub timestamp
    """
    feed = api(f"notifications?all=false&since={since}&per_page=100")
    if not isinstance(feed, list):
        return []
    raised = [notification for notification in feed if notification.get("reason") in ("mention", "team_mention")]
    # One request each to find out who wrote them and whose thread it is, so only the first few are traced and
    # those go out together. Whose thread it is cannot come from the poll's own lists: a mention often lands on a
    # pull request the user neither wrote nor reviews, or on one already closed.
    traced = [
        (notification.get("subject") or {}).get("latest_comment_url") or ""
        for notification in raised[:MENTION_LOOKUP_LIMIT]
    ]
    threads = [(notification.get("subject") or {}).get("url") or "" for notification in raised[:MENTION_LOOKUP_LIMIT]]
    with ThreadPoolExecutor(max_workers=CONCURRENT_LOOKUPS, thread_name_prefix="gh-tray-mentions") as pool:
        authors = list(pool.map(comment_author, traced))
        owners = list(pool.map(thread_author, threads))
    authors += [""] * (len(raised) - len(authors))
    owners += [""] * (len(raised) - len(owners))

    mentions = []
    for notification, actor, owner in zip(raised, authors, owners, strict=True):
        subject = notification.get("subject") or {}
        address = subject.get("url") or ""
        mentions.append(
            {
                "repo": nested(notification, "repository", "full_name"),
                # The number is the tail of the thread's address, which is the one place the feed carries it.
                "number": address.rstrip("/").rsplit("/", 1)[-1]
                if address.rstrip("/").rsplit("/", 1)[-1].isdigit()
                else "",
                "title": subject.get("title") or "",
                "type": subject.get("type") or "",
                "reason": notification.get("reason") or "mention",
                "updatedAt": notification.get("updated_at") or "",
                "url": page_url(address),
                "actor": actor,
                "author": owner,
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
    cutoff = config.get("max_age_days", 0)
    authored_search = search_for(AUTHORED, cutoff, started)
    reviewing_search = search_for(REVIEWING, cutoff, started)
    closed_search = search_for(CLOSED, CLOSED_LOOKBACK_DAYS, started)
    try:
        # The askings do not depend on one another, and each spends nearly all its time waiting on GitHub, so
        # they go out together. The poll then takes about as long as its slowest part rather than their sum.
        with ThreadPoolExecutor(max_workers=CONCURRENT_ASKINGS, thread_name_prefix="gh-tray-collect") as pool:
            signed_in = pool.submit(viewer)
            own = pool.submit(search_pull_requests, SEARCH_QUERY, authored_search)
            to_review = pool.submit(search_pull_requests, SEARCH_QUERY, reviewing_search)
            finished = pool.submit(search_pull_requests, SEARCH_QUERY, closed_search)
            mentioning = pool.submit(collect_mentions, since)
            signed_in_as = signed_in.result()
            authored = [normalise(node, "authored") for node in own.result()]
            reviewing = [normalise(node, "reviewing") for node in to_review.result()]
            closed = [normalise(node, "closed") for node in finished.result()]
            mentions = mentioning.result()
    except GitHubError as error:
        write_text_atomic(ERROR_LOG_PATH, f"{started.isoformat()}\n{error}\n")
        logger.error("collection failed: {}", error)
        return None, str(error)[:120]

    authored, hidden_authored = drop_stale(authored, cutoff, started)
    reviewing, hidden_reviewing = drop_stale(reviewing, cutoff, started)
    write_json_atomic(STATE_PATH, {"lastRunAt": started.strftime(TIMESTAMP_FORMAT)})
    logger.info(
        "collected {} authored and {} awaiting review, {} closed, {} mention(s), {} hidden as stale",
        len(authored),
        len(reviewing),
        len(closed),
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
        "closed": closed,
        "mentions": mentions,
    }, ""
