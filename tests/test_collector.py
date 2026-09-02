"""Turning what GitHub returns into the flat records the rest of the application reads.

GitHub omits a great deal rather than nulling it, and a login can go missing at any level: a deleted account, a
commit by somebody with no GitHub user, a pull request with no reviews yet. So most of these cases are about a
reply that is missing something, which is the normal case rather than the exception.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gh_tray import collector, github

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def node(**overrides) -> dict:
    """Build a pull request as GitHub returns it, complete unless overridden."""
    found = {
        "number": 7,
        "title": "Add a widget",
        "url": "https://github.com/acme/widget/pull/7",
        "isDraft": False,
        "createdAt": "2026-05-01T00:00:00Z",
        "updatedAt": "2026-05-30T00:00:00Z",
        "totalCommentsCount": 3,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "repository": {"nameWithOwner": "acme/widget"},
        "author": {"login": "someone"},
        "commits": {
            "nodes": [
                {"commit": {"statusCheckRollup": {"state": "SUCCESS"}, "author": {"user": {"login": "committer"}}}}
            ]
        },
        "latestReviews": {"nodes": [{"author": {"login": "reviewer"}}]},
        "comments": {"nodes": [{"author": {"login": "commenter"}, "createdAt": "2026-05-30T00:00:00Z"}]},
        "reviews": {
            "nodes": [
                {
                    "author": {"login": "reviewer"},
                    "comments": {"nodes": [{"createdAt": "2026-05-29T00:00:00Z", "replyTo": None}]},
                }
            ]
        },
    }
    found.update(overrides)
    return found


def test_a_complete_pull_request_is_read_in_full():
    record = collector.normalise(node(), "authored")
    assert record["key"] == "acme/widget#7"
    assert record["side"] == "authored"
    assert record["ci"] == "SUCCESS"
    assert record["reviewDecision"] == "APPROVED"
    assert (record["lastCommitBy"], record["lastReviewBy"], record["lastCommentBy"]) == (
        "committer",
        "reviewer",
        "commenter",
    )


def test_a_pull_request_with_no_reviews_or_comments_still_reads():
    record = collector.normalise(
        node(latestReviews={"nodes": []}, comments={"nodes": []}, reviews={"nodes": []}), "reviewing"
    )
    assert record["lastReviewBy"] == ""
    assert record["lastCommentBy"] == ""


def margin(login: str, at: str, answering: str = "") -> dict:
    """Build the last review as GitHub returns it, holding one comment against the diff."""
    reply_to = {"author": {"login": answering}} if answering else None
    return {"nodes": [{"author": {"login": login}, "comments": {"nodes": [{"createdAt": at, "replyTo": reply_to}]}}]}


def test_a_pull_request_commented_on_only_in_the_margin_still_names_who_did_it():
    # GitHub counts comments against the diff but does not list them, so a pull request reviewed entirely that way
    # used to have a comment count that moved with nobody to name for it.
    margin_only = node(comments={"nodes": []}, reviews=margin("MattAmos", "2026-05-30T00:00:00Z"))
    assert collector.normalise(margin_only, "authored")["lastCommentBy"] == "MattAmos"


def test_the_state_of_a_pull_request_travels_through():
    assert collector.normalise(node(), "authored")["state"] == "OPEN"
    assert collector.normalise(node(state="MERGED"), "closed")["state"] == "MERGED"


def test_collecting_asks_for_closed_pull_requests_within_a_bounded_window(monkeypatch, tmp_path):
    searches: list[str] = []
    monkeypatch.setattr(collector, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(collector, "viewer", lambda: "me")
    monkeypatch.setattr(collector, "collect_mentions", lambda _since, _hidden: [])

    def fake_search(_query: str, q: str) -> list[dict]:
        searches.append(q)
        return [node(state="MERGED")] if "is:closed" in q else []

    monkeypatch.setattr(collector, "search_pull_requests", fake_search)
    digest, error = collector.collect({"max_age_days": 365})
    assert error == "" and digest is not None
    closed_search = next(q for q in searches if "is:closed" in q)
    # Newest first and bounded to its own short window, whatever the age cutoff: the search stops after a few
    # pages, and either omission would let a freshly closed pull request fall off the end of the results.
    assert "sort:updated-desc" in closed_search
    stamp = closed_search.rsplit("updated:>", 1)[1].split()[0]
    window = datetime.now(UTC) - datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=UTC)
    assert window <= timedelta(days=collector.CLOSED_LOOKBACK_DAYS + 1)
    assert [entry["side"] for entry in digest["closed"]] == ["closed"]
    assert digest["closed"][0]["state"] == "MERGED"


def test_whichever_of_the_two_came_last_is_the_one_named():
    later_review = node(reviews=margin("reviewer", "2026-05-31T00:00:00Z"))
    assert collector.normalise(later_review, "authored")["lastCommentBy"] == "reviewer"
    assert collector.normalise(node(), "authored")["lastCommentBy"] == "commenter"


def test_a_review_without_comments_in_the_margin_never_outranks_the_conversation():
    # A review submitted with only a summary body holds nothing in the margin, so it carries no time and never wins.
    bodyless = node(reviews={"nodes": [{"author": {"login": "reviewer"}, "comments": {"nodes": []}}]})
    assert collector.normalise(bodyless, "authored")["lastCommentBy"] == "commenter"


def test_a_reply_in_the_margin_names_whose_comment_it_answers():
    answering = node(comments={"nodes": []}, reviews=margin("author", "2026-05-31T00:00:00Z", answering="SamRWest"))
    assert collector.normalise(answering, "reviewing")["lastCommentAnswers"] == "SamRWest"


def test_a_comment_that_answers_nobody_reads_as_answering_nobody():
    assert collector.normalise(node(), "reviewing")["lastCommentAnswers"] == ""
    thread_start = node(comments={"nodes": []}, reviews=margin("author", "2026-05-31T00:00:00Z"))
    assert collector.normalise(thread_start, "reviewing")["lastCommentAnswers"] == ""


def test_a_pull_request_with_no_checks_reads_as_having_none():
    record = collector.normalise(
        node(commits={"nodes": [{"commit": {"statusCheckRollup": None, "author": None}}]}), "authored"
    )
    assert record["ci"] == "NO_CHECKS"
    assert record["lastCommitBy"] == ""


def test_a_commit_by_somebody_with_no_github_account_names_nobody():
    unlinked = {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}, "author": {"user": None}}}]}
    record = collector.normalise(node(commits=unlinked), "authored")
    assert record["lastCommitBy"] == ""
    assert record["ci"] == "SUCCESS"


def test_a_deleted_author_is_reported_as_unknown():
    assert collector.normalise(node(author=None), "authored")["author"] == "unknown"


def test_mergeability_github_has_not_worked_out_reads_as_unknown():
    assert collector.normalise(node(mergeable=None), "authored")["mergeable"] == "UNKNOWN"


def test_a_reply_missing_everything_optional_still_produces_a_record():
    record = collector.normalise({"number": 7, "repository": {"nameWithOwner": "acme/widget"}}, "authored")
    assert record["key"] == "acme/widget#7"
    assert record["ci"] == "NO_CHECKS"
    assert record["comments"] == 0
    assert record["title"] == ""


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("author", "login"), "someone"),
        (("commits", "nodes", "commit", "author", "user", "login"), "committer"),
        (("nothing", "here"), ""),
        (("latestReviews", "nodes", "author", "login"), "reviewer"),
    ],
)
def test_following_a_chain_of_keys_stops_at_the_first_gap(path, expected):
    assert collector.nested(node(), *path) == expected


def test_following_a_chain_into_an_empty_list_gives_nothing():
    assert collector.nested(node(latestReviews={"nodes": []}), "latestReviews", "nodes", "author", "login") == ""


def older(days: int) -> dict:
    """Build a record last touched a given number of days before the reference moment."""
    return {"updatedAt": (NOW - timedelta(days=days)).strftime(collector.TIMESTAMP_FORMAT)}


def test_long_abandoned_pull_requests_are_dropped():
    kept, hidden = collector.drop_stale([older(10), older(400)], max_age_days=365, now=NOW)
    assert len(kept) == 1
    assert hidden == 1


def test_a_cutoff_of_zero_keeps_everything():
    kept, hidden = collector.drop_stale([older(10), older(4000)], max_age_days=0, now=NOW)
    assert len(kept) == 2
    assert hidden == 0


def test_a_pull_request_touched_today_is_never_dropped():
    kept, hidden = collector.drop_stale([older(0)], max_age_days=1, now=NOW)
    assert len(kept) == 1
    assert hidden == 0


def test_an_interface_address_becomes_a_page_a_person_can_open():
    assert (
        collector.page_url("https://api.github.com/repos/acme/widget/pulls/7")
        == "https://github.com/acme/widget/pull/7"
    )


def test_an_address_that_is_already_a_page_is_left_alone():
    assert collector.page_url("https://github.com/acme/widget/pull/7") == "https://github.com/acme/widget/pull/7"


def test_no_address_at_all_is_harmless():
    assert collector.page_url("") == ""


def test_the_error_line_is_preferred_over_trailing_usage_help():
    # The last line of a failed call is often generic help, which tells the user nothing about what went wrong.
    stderr = "error: HTTP 502 Service Unavailable\nUsage: gh api <endpoint>\nRun 'gh api --help' for more information."
    assert github.first_error_line(stderr).startswith("error: HTTP 502")


def test_the_first_line_is_used_when_nothing_mentions_an_error():
    assert github.first_error_line("something odd happened\nand then stopped") == "something odd happened"


def test_empty_error_output_still_describes_the_failure():
    assert github.first_error_line("") == "the GitHub CLI failed without saying why"
    assert github.first_error_line("  \n \n") == "the GitHub CLI failed without saying why"


def test_a_long_error_is_shortened():
    assert len(github.first_error_line("error: " + "x" * 500)) <= 120


def test_unreadable_output_is_reported_as_such():
    with pytest.raises(github.GitHubError, match="came back unreadable"):
        github.parse("{ not json", "the search")


def test_a_reply_carrying_only_errors_is_retried_and_then_reported(monkeypatch):
    attempts = []

    def failing(_arguments, timeout=0):
        attempts.append(1)
        return '{"errors": [{"message": "something was too expensive"}]}'

    monkeypatch.setattr(github, "run", failing)
    monkeypatch.setattr(github.time, "sleep", lambda _seconds: None)
    with pytest.raises(github.GitHubError, match="too expensive"):
        github.graphql("query", {"q": "is:pr"})
    assert len(attempts) == github.RETRY_ATTEMPTS


def test_a_plain_fetch_that_works_on_the_second_attempt_is_accepted(monkeypatch):
    # A failed lookup used to be swallowed after one attempt, which is how a mention lost its author for good.
    answers = [github.GitHubError("something transient"), '{"user": {"login": "someone"}}']

    def flaky(_arguments, timeout=0):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(github, "run", flaky)
    monkeypatch.setattr(github.time, "sleep", lambda _seconds: None)
    assert github.api("user") == {"user": {"login": "someone"}}


def test_a_fetch_of_something_deleted_is_not_retried(monkeypatch):
    # Asking again for a deleted comment gets the same answer, and each retry would sleep between attempts.
    attempts = []

    def gone(_arguments, timeout=0):
        attempts.append(1)
        raise github.GitHubError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(github, "run", gone)
    with pytest.raises(github.GitHubError, match="404"):
        github.api("repos/acme/widget/issues/comments/1")
    assert len(attempts) == 1


def test_being_asked_to_slow_down_is_the_one_client_error_worth_retrying():
    assert github.looks_permanent("gh: Not Found (HTTP 404)") is True
    assert github.looks_permanent("gh: was submitted too quickly (HTTP 429)") is False
    assert github.looks_permanent("HTTP 502 bad gateway") is False


def test_a_reply_that_works_on_the_second_attempt_is_accepted(monkeypatch):
    replies = ['{"errors": [{"message": "busy"}]}', '{"data": {"search": {"nodes": []}}}']
    monkeypatch.setattr(github, "run", lambda _arguments, timeout=0: replies.pop(0))
    monkeypatch.setattr(github.time, "sleep", lambda _seconds: None)
    assert github.graphql("query", {"q": "is:pr"}) == {"search": {"nodes": []}}


def test_every_page_of_results_is_collected(monkeypatch):
    pages = [
        {"search": {"nodes": [node(number=1)], "pageInfo": {"hasNextPage": True, "endCursor": "a"}}},
        {"search": {"nodes": [node(number=2)], "pageInfo": {"hasNextPage": False, "endCursor": ""}}},
    ]
    monkeypatch.setattr(github, "graphql", lambda _query, _variables: pages.pop(0))
    assert [found["number"] for found in github.search_pull_requests("query", "is:pr")] == [1, 2]


def test_paging_stops_rather_than_running_forever(monkeypatch):
    endless = {"search": {"nodes": [node()], "pageInfo": {"hasNextPage": True, "endCursor": "always"}}}
    monkeypatch.setattr(github, "graphql", lambda _query, _variables: endless)
    assert len(github.search_pull_requests("query", "is:pr")) == github.MAX_PAGES


def test_something_that_is_not_a_pull_request_is_ignored(monkeypatch):
    mixed = {"search": {"nodes": [node(), {}], "pageInfo": {"hasNextPage": False}}}
    monkeypatch.setattr(github, "graphql", lambda _query, _variables: mixed)
    assert len(github.search_pull_requests("query", "is:pr")) == 1


def test_a_mention_carries_the_number_its_thread_address_ends_in(monkeypatch):
    feed = [
        {
            "reason": "mention",
            "repository": {"full_name": "acme/widget"},
            "subject": {
                "title": "look at this",
                "url": "https://api.github.com/repos/acme/widget/pulls/217",
                "type": "PullRequest",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "reason": "team_mention",
            "repository": {"full_name": "acme/widget"},
            "subject": {
                "title": "a discussion with no number",
                "url": "https://api.github.com/repos/acme/widget",
                "type": "Repository",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]
    monkeypatch.setattr(collector, "api", lambda _path: feed)
    monkeypatch.setattr(collector, "comment_author", lambda _url: "")
    found = collector.collect_mentions("2026-01-01T00:00:00Z")
    assert found[0]["number"] == "217"
    assert found[1]["number"] == ""


def test_a_hidden_organisation_is_left_out_of_a_search():
    now = datetime(2026, 1, 31, tzinfo=UTC)
    assert collector.search_for("is:pr", 0, now, ["acme", "widgets"]) == "is:pr -org:acme -org:widgets"
    assert collector.search_for("is:pr", 30, now, ["acme"]) == "is:pr updated:>2026-01-01 -org:acme"
    assert collector.search_for("is:pr", 0, now) == "is:pr"


def test_mentions_from_a_hidden_organisation_are_left_out(monkeypatch):
    feed = [
        {
            "reason": "mention",
            "repository": {"full_name": "acme/widget", "owner": {"login": "acme"}},
            "subject": {"url": "", "title": "a"},
        },
        {
            "reason": "mention",
            "repository": {"full_name": "other/thing", "owner": {"login": "Other"}},
            "subject": {"url": "", "title": "b"},
        },
    ]
    monkeypatch.setattr(collector, "api", lambda _path: feed)
    monkeypatch.setattr(collector, "comment_author", lambda _url: "")
    monkeypatch.setattr(collector, "thread_author", lambda _url: "")
    found = collector.collect_mentions("2026-01-01T00:00:00Z", ["other"])
    assert [mention["repo"] for mention in found] == ["acme/widget"]


def test_the_organisations_an_account_belongs_to_are_listed_alphabetically(monkeypatch):
    monkeypatch.setattr(
        github, "api", lambda _path: [{"login": "Widgets"}, {"login": "acme"}, {"nope": 1}, {"login": ""}]
    )
    assert github.organisations() == ["acme", "Widgets"]


def test_no_organisations_reads_as_an_empty_list(monkeypatch):
    monkeypatch.setattr(github, "api", lambda _path: {"message": "odd"})
    assert github.organisations() == []
