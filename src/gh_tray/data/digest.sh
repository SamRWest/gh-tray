#!/usr/bin/env bash
# Collect a GitHub activity digest for the authenticated gh user and emit it as a single JSON document.
#
# Detects continuous-integration (CI) status changes by diffing the current check-rollup state against a snapshot
# written on the previous run. The first run has no baseline, so it reports the baseline as established instead of
# inventing changes.
#
# Writes three files beside the state file: the state snapshot itself, `latest.json` holding the full digest, and
# `summary.json` holding a compact tally. Both defaults may be overridden, which is how a wider reporting window is
# rendered without disturbing the live baseline.
#
# Usage: digest.sh [state-file] [comma-separated-orgs] [max-age-days]
#
# The state file defaults to the working directory, and an empty organisation list skips the newly-opened sweep, so
# the script carries nothing about any particular account or machine.
set -uo pipefail

STATE_FILE="${1:-./digest-state.json}"
ORGS_CSV="${2:-}"
MAX_AGE_DAYS="${3:-365}"
if [[ ! "$MAX_AGE_DAYS" =~ ^[0-9]+$ ]]; then
  echo "{\"error\":\"max-age-days must be a whole number of days, or 0 for no cutoff, not \\\"$MAX_AGE_DAYS\\\"\"}"
  exit 1
fi
mkdir -p "$(dirname "$STATE_FILE")"

# Windows caps a command line near 32 KB and the pull request payload outgrew it, so anything that scales with the
# number of pull requests reaches jq through a file or standard input rather than through an argument.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PREV_CI='{}'
SINCE=''

if [[ -f "$STATE_FILE" ]]; then
  SINCE="$(jq -r '.lastRunAt // empty' "$STATE_FILE")"
  PREV_CI="$(jq -c '.ci // {}' "$STATE_FILE")"
fi
# No prior run means no baseline, so fall back to a one-day window.
FIRST_RUN=false
if [[ -z "$SINCE" ]]; then
  SINCE="$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)"
  FIRST_RUN=true
fi

# Fetching check-rollup state alongside a search is expensive enough that GitHub intermittently returns HTTP 502,
# so each search runs as its own request and is retried with backoff.
GQL='
  query($q: String!, $cursor: String) {
    search(query: $q, type: ISSUE, first: 40, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on PullRequest {
          number title url isDraft createdAt updatedAt totalCommentsCount mergeable
          repository { nameWithOwner }
          author { login }
          reviewDecision
          commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        }
      }
    }
  }'

# Pages in batches of 40 because the result set exceeds any single page, and a larger page size provokes 502s.
gql_search() {
  local search="$1" cursor='' page raw attempt all='[]'
  for page in 1 2 3 4 5; do
    raw=''
    for attempt in 1 2 3; do
      if [[ -z "$cursor" ]]; then
        raw="$(gh api graphql -f query="$GQL" -f q="$search" 2>/dev/null)"
      else
        raw="$(gh api graphql -f query="$GQL" -f q="$search" -f cursor="$cursor" 2>/dev/null)"
      fi
      [[ -n "$raw" ]] && break
      sleep $((attempt * 3))
    done
    [[ -z "$raw" ]] && return 1
    all="$(jq -c --argjson acc "$all" '$acc + [.data.search.nodes[] | select(.number != null)]' <<<"$raw")"
    [[ "$(jq -r '.data.search.pageInfo.hasNextPage' <<<"$raw")" == "true" ]] || break
    cursor="$(jq -r '.data.search.pageInfo.endCursor' <<<"$raw")"
  done
  printf '%s' "$all"
}

normalise_prs() {
  jq -c '[ .[] | {
    key: (.repository.nameWithOwner + "#" + (.number|tostring)),
    repo: .repository.nameWithOwner,
    number, title, url, isDraft, createdAt, updatedAt,
    author: (.author.login // "unknown"),
    comments: (.totalCommentsCount // 0),
    reviewDecision: (.reviewDecision // "NONE"),
    mergeable: (.mergeable // "UNKNOWN"),
    ci: (.commits.nodes[0].commit.statusCheckRollup.state // "NO_CHECKS")
  } ]'
}

AUTHORED_RAW="$(gql_search 'is:pr is:open author:@me archived:false')" \
  || { echo '{"error":"authored pull request query failed after retries"}'; exit 1; }
REVIEWING_RAW="$(gql_search 'is:pr is:open review-requested:@me archived:false')" \
  || { echo '{"error":"review-requested pull request query failed after retries"}'; exit 1; }

PRS="$(jq -c --argjson a "$(normalise_prs <<<"$AUTHORED_RAW")" \
              --argjson r "$(normalise_prs <<<"$REVIEWING_RAW")" -n '{authored: $a, reviewing: $r}')"

# Long-abandoned pull requests crowd out the ones anyone will act on, so they are dropped up front. Filtering here
# rather than at render time keeps the tables, the check snapshot and the change diff describing the same set.
# A pull request is never updated before it is created, so an update cutoff already excludes everything created
# earlier still; filtering on creation as well would only discard old branches that are still being worked on.
STALE='{"maxAgeDays": 0, "hiddenAuthored": 0, "hiddenReviewing": 0}'
if [[ "$MAX_AGE_DAYS" != "0" ]]; then
  CUTOFF="$(date -u -d "$MAX_AGE_DAYS days ago" +%Y-%m-%dT%H:%M:%SZ)"
  STALE="$(jq -c --arg cutoff "$CUTOFF" --argjson days "$MAX_AGE_DAYS" '{
    maxAgeDays: $days,
    hiddenAuthored: ([.authored[] | select(.updatedAt < $cutoff)] | length),
    hiddenReviewing: ([.reviewing[] | select(.updatedAt < $cutoff)] | length)
  }' <<<"$PRS")"
  PRS="$(jq -c --arg cutoff "$CUTOFF" 'map_values(map(select(.updatedAt >= $cutoff)))' <<<"$PRS")"
fi

# "My repos" means the ones I own or am an explicit collaborator on. Plain organisation membership is deliberately
# excluded: an org-wide search returns every new pull request in every repo I can merely see, which is noise.
# An intermittent failure here yields no output at all rather than an empty array, which would then reach --argjson
# below as an empty string and abort the whole run. Falling back to an empty list costs only the newly-opened
# section on that run.
MY_REPOS="$(gh api --paginate "user/repos?affiliation=owner,collaborator&per_page=100" \
  --jq '[.[].full_name]' 2>/dev/null | jq -sc 'add // []' 2>/dev/null)"
MY_REPOS="${MY_REPOS:-[]}"

# Newly opened pull requests across the named orgs, restricted to the window since the previous run. The org search is
# the only way to sweep many repos in one request, so the repo filter is applied to its results afterwards.
NEW_PRS='[]'
IFS=',' read -r -a ORGS <<<"$ORGS_CSV"
for org in "${ORGS[@]}"; do
  org="$(tr -d '[:space:]' <<<"$org")"
  [[ -z "$org" ]] && continue
  found="$(gh search prs --owner="$org" --created=">$SINCE" --state=open --limit 40 \
    --json repository,number,title,url,createdAt,updatedAt,author,isDraft,commentsCount 2>/dev/null)" || found='[]'
  NEW_PRS="$(jq -c --argjson b "${found:-[]}" '
    . + [$b[] | {
      repo: .repository.nameWithOwner, number, title, url, isDraft, createdAt, updatedAt,
      author: (.author.login // "unknown"),
      comments: (.commentsCount // 0)
    }]' <<<"$NEW_PRS")"
done
NEW_PRS="$(jq -c --argjson mine "$MY_REPOS" '
  ($mine | map({key: ., value: true}) | from_entries) as $allow
  | [ .[] | select($allow[.repo] == true) ]' <<<"$NEW_PRS")"

# Mentions come from the notifications feed, which the repo scope already covers.
NOTIFS="$(gh api "notifications?all=false&since=$SINCE&per_page=100" 2>/dev/null)" || NOTIFS='[]'
MENTIONS="$(jq -c '[ .[]
  | select(.reason == "mention" or .reason == "team_mention")
  | { repo: .repository.full_name, title: .subject.title, type: .subject.type,
      reason: .reason, updatedAt: .updated_at,
      url: (.subject.url // "" | sub("api\\.github\\.com/repos"; "github.com") | sub("/pulls/"; "/pull/")) }
]' <<<"${NOTIFS:-[]}")"

# Diff the current rollup state against the previous snapshot.
CUR_CI="$(jq -c '[(.authored + .reviewing)[] | {key: .key, value: .ci}] | from_entries' <<<"$PRS")"
CI_CHANGES="$(jq -c --argjson prev "$PREV_CI" --argjson cur "$CUR_CI" -n '
  [ $cur | to_entries[]
    | select($prev[.key] != null and $prev[.key] != .value)
    | { pr: .key, from: $prev[.key], to: .value } ]')"

printf '%s' "$PRS" >"$TMP_DIR/prs.json"
printf '%s' "$NEW_PRS" >"$TMP_DIR/new-prs.json"
printf '%s' "$MENTIONS" >"$TMP_DIR/mentions.json"

DIGEST="$(jq -n --slurpfile prsIn "$TMP_DIR/prs.json" --slurpfile newPrsIn "$TMP_DIR/new-prs.json" \
      --slurpfile mentionsIn "$TMP_DIR/mentions.json" \
      --argjson ciChanges "$CI_CHANGES" --arg since "$SINCE" --arg now "$NOW" \
      --argjson firstRun "$FIRST_RUN" --argjson stale "$STALE" '
  def by_recent: sort_by(.updatedAt) | reverse;
  # Ages are humanised here rather than at render time so every consumer reads the same wording. They are relative to
  # the window end, which is what a later cached re-render must state.
  def ago($t): (($t - fromdateiso8601) / 86400) as $d
    | if   $d < 1   then "today"
      elif $d < 2   then "1 day ago"
      elif $d < 14  then "\($d|floor) days ago"
      elif $d < 60  then "\(($d/7)|floor) weeks ago"
      elif $d < 365 then "\(($d/30.44)|floor) months ago"
      elif $d < 730 then "1 year ago"
      else               "\(($d/365.25)|floor) years ago"
      end;
  def with_age($t): map(. + {updatedAge: (.updatedAt | ago($t))}
                          + (if .createdAt then {createdAge: (.createdAt | ago($t))} else {} end));
  ($now | fromdateiso8601) as $t
  | $prsIn[0] as $prs | $newPrsIn[0] as $newPrs | $mentionsIn[0] as $mentions
  | { window: {since: $since, until: $now}, firstRun: $firstRun, staleFilter: $stale,
    authored: ($prs.authored | by_recent | with_age($t)), reviewing: ($prs.reviewing | by_recent | with_age($t)),
    newlyOpened: ($newPrs | unique_by(.repo + "#" + (.number|tostring)) | by_recent | with_age($t)),
    mentions: ($mentions | by_recent | with_age($t)), ciChanges: $ciChanges }')"

# A render that failed or came back truncated must not overwrite the cached digest or advance the baseline, both of
# which happen further down and are hard to notice going wrong.
if ! jq -e '.window.until' >/dev/null 2>&1 <<<"$DIGEST"; then
  echo '{"error":"digest assembly failed"}'
  exit 1
fi

DEST_DIR="$(dirname "$STATE_FILE")"

# Keep the full digest and a compact tally beside the state file so a later session can show the last result without
# repeating any API calls.
printf '%s\n' "$DIGEST" >"$DEST_DIR/latest.json"
cat "$DEST_DIR/latest.json"
jq -c '{
  at: .window.until,
  red: ([.authored[], .reviewing[] | select(.ci == "FAILURE" or .ci == "ERROR")] | length),
  pending: ([.authored[], .reviewing[] | select(.ci == "PENDING")] | length),
  reviews: (.reviewing | length),
  mentions: (.mentions | length),
  newPrs: (.newlyOpened | length),
  ciChanges: (.ciChanges | length)
}' "$DEST_DIR/latest.json" >"$DEST_DIR/summary.json"

# Persist the snapshot last so a failure above leaves the previous baseline intact.
jq -n --arg now "$NOW" --argjson ci "$CUR_CI" '{lastRunAt: $now, ci: $ci}' >"$STATE_FILE"
