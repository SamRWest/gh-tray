"""Gathers everything the application needs from GitHub, in one pass; deciding what changed happens elsewhere."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from loguru import logger

from .config import ERROR_LOG_PATH, HIDDEN_OWNERS_KEY, INVOLVED_KEY, STATE_PATH, WATCH_OTHERS_KEY, WATCHED_OWNERS_KEY
from .github import GitHubError, api, search_pull_requests, viewer
from .storage import read_json, write_json_atomic, write_text_atomic

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FIRST_RUN_WINDOW = timedelta(days=1)
# A mention often lands on a pull request neither authored nor reviewed, so only the first few are traced back.
MENTION_LOOKUP_LIMIT = 10
# GitHub asks callers not to flood it; four overlaps the waiting without becoming a flood.
CONCURRENT_ASKINGS = 4
CONCURRENT_LOOKUPS = 4

AUTHORED = "is:pr is:open author:@me archived:false"
REVIEWING = "is:pr is:open review-requested:@me archived:false"
# Listed only when asked; drops whatever the other two searches already found, so each lands on one side only.
INVOLVED = "is:pr is:open involves:@me archived:false"
# Sorted newest first, since GitHub's best-match order can drop a freshly closed pull request from the results.
CLOSED = "is:pr is:closed involves:@me archived:false sort:updated-desc"
# Anything older has already left the event log's trimmed tail, regardless of the age cutoff.
CLOSED_LOOKBACK_DAYS = 30


def search_for(base: str, max_age_days: int, now: datetime, hidden_owners: list[str] | None = None) -> str:
    """Add the age cutoff and excluded owners (without it, a long-lived account pays for pages it then discards).

    :param base: the search expression
    :param max_age_days: how old is too old, or zero to keep everything
    :param now: the moment to measure against
    :param hidden_owners: the owners, people or organisations, whose pull requests are not wanted
    """
    qualifiers = [base]
    if max_age_days:
        qualifiers.append(f"updated:>{(now - timedelta(days=max_age_days)).strftime('%Y-%m-%d')}")
    # A hyphen excludes a qualifier; a login could name a person or an organisation, so both are said.
    for login in hidden_owners or []:
        qualifiers += [f"-user:{login}", f"-org:{login}"]
    return " ".join(qualifiers)


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
    """Follow a chain of keys, returning an empty string the moment one is missing (GitHub omits absent fields).

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
    """Return whether the newest comment sits against the diff (the comment list holds only the conversation half).

    :param node: a pull request as GitHub returned it
    """
    said_at = nested(node, "comments", "nodes", "createdAt")
    margin_at = nested(node, "reviews", "nodes", "comments", "nodes", "createdAt")
    return margin_at > said_at


def last_commenter(node: dict) -> str:
    """Return whoever commented last on a pull request, in the conversation or against the diff.

    :param node: a pull request as GitHub returned it
    :return: a login, or an empty string when neither can be found
    """
    if newest_comment_is_marginal(node):
        return nested(node, "reviews", "nodes", "author", "login")
    return nested(node, "comments", "nodes", "author", "login")


def last_comment_answers(node: dict) -> str:
    """Return whose comment the newest comment answers (only a diff comment can; conversation ones stand alone).

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


def owned_by(records: list[dict], owners: list[str]) -> list[dict]:
    """Return the records belonging to some owners (filtered here, since a login may be a person's or an org's).

    :param records: pull requests or mentions, each naming its repository
    :param owners: the logins whose repositories are wanted
    """
    wanted = {login.casefold() for login in owners}
    return [record for record in records if str(record.get("repo", "")).partition("/")[0].casefold() in wanted]


def drop_stale(pull_requests: list[dict], max_age_days: int, now: datetime) -> tuple[list[dict], int]:
    """Remove pull requests nobody has touched for a long time (last-updated only, to keep active old branches).

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


def collect_mentions(since: str, hidden_owners: list[str] | None = None) -> list[dict]:
    """Return the mentions raised since a moment, each named with whoever wrote it where that can be found.

    :param since: the earliest moment to report, as a GitHub timestamp
    :param hidden_owners: the owners, people or organisations, whose mentions are not wanted
    """
    feed = api(f"notifications?all=false&since={since}&per_page=100")
    if not isinstance(feed, list):
        return []
    left_out = {login.casefold() for login in hidden_owners or []}
    raised = [
        notification
        for notification in feed
        if notification.get("reason") in ("mention", "team_mention")
        and nested(notification, "repository", "owner", "login").casefold() not in left_out
    ]
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
                # The thread's address is the one place the feed carries the pull request number.
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
    hidden = config.get(HIDDEN_OWNERS_KEY) or []
    authored_search = search_for(AUTHORED, cutoff, started, hidden)
    reviewing_search = search_for(REVIEWING, cutoff, started, hidden)
    closed_search = search_for(CLOSED, CLOSED_LOOKBACK_DAYS, started, hidden)
    involved_search = search_for(INVOLVED, cutoff, started, hidden) if config.get(INVOLVED_KEY) else ""
    logger.debug(
        "searching for {!r}, {!r}, {!r} and {!r}, and mentions since {}",
        authored_search,
        reviewing_search,
        closed_search,
        involved_search or "nothing else",
        since,
    )
    try:
        with ThreadPoolExecutor(max_workers=CONCURRENT_ASKINGS, thread_name_prefix="gh-tray-collect") as pool:
            signed_in = pool.submit(viewer)
            own = pool.submit(search_pull_requests, SEARCH_QUERY, authored_search)
            to_review = pool.submit(search_pull_requests, SEARCH_QUERY, reviewing_search)
            finished = pool.submit(search_pull_requests, SEARCH_QUERY, closed_search)
            also = pool.submit(search_pull_requests, SEARCH_QUERY, involved_search) if involved_search else None
            mentioning = pool.submit(collect_mentions, since, hidden)
            signed_in_as = signed_in.result()
            authored = [normalise(node, "authored") for node in own.result()]
            reviewing = [normalise(node, "reviewing") for node in to_review.result()]
            closed = [normalise(node, "closed") for node in finished.result()]
            involved = [normalise(node, "involved") for node in also.result()] if also is not None else []
            mentions = mentioning.result()
    except GitHubError as error:
        write_text_atomic(ERROR_LOG_PATH, f"{started.isoformat()}\n{error}\n")
        logger.error("collection failed: {}", error)
        return None, str(error)[:120]

    authored, hidden_authored = drop_stale(authored, cutoff, started)
    reviewing, hidden_reviewing = drop_stale(reviewing, cutoff, started)
    on_a_side_already = {entry["key"] for entry in authored + reviewing}
    involved, _hidden_involved = drop_stale(
        [entry for entry in involved if entry["key"] not in on_a_side_already], cutoff, started
    )
    if not config.get(WATCH_OTHERS_KEY, True):
        kept = config.get(WATCHED_OWNERS_KEY) or []
        authored, reviewing, involved = owned_by(authored, kept), owned_by(reviewing, kept), owned_by(involved, kept)
        closed, mentions = owned_by(closed, kept), owned_by(mentions, kept)
    write_json_atomic(STATE_PATH, {"lastRunAt": started.strftime(TIMESTAMP_FORMAT)})
    logger.info(
        "collected {} authored and {} awaiting review, {} closed, {} involved, {} mention(s), {} hidden as stale",
        len(authored),
        len(reviewing),
        len(closed),
        len(involved),
        len(mentions),
        hidden_authored + hidden_reviewing,
    )
    return {
        "window": {"since": since, "until": started.strftime(TIMESTAMP_FORMAT)},
        "staleFilter": {"maxAgeDays": cutoff, "hiddenAuthored": hidden_authored, "hiddenReviewing": hidden_reviewing},
        "viewer": signed_in_as,
        "authored": authored,
        "reviewing": reviewing,
        "closed": closed,
        "involved": involved,
        "mentions": mentions,
    }, ""
