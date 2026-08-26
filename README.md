# gh-tray

A system tray icon that watches your GitHub pull requests, tells you when something changes, and opens a terminal
dashboard when you want the full picture.

The icon carries a count of changes you have not seen yet.

| Doing this to the icon | Gets you                                                                         |
| ---------------------- | -------------------------------------------------------------------------------- |
| Hovering               | A short status summary                                                           |
| Clicking once          | A small window listing the most recent changes; click a row to open it on GitHub |
| Clicking twice         | [gh-dash](https://github.com/dlvhdr/gh-dash), maximised                          |
| Right-clicking         | The review queue, the login-start switch, the settings window and the rest       |

A single click cannot act the moment it happens, because the first click of a double click looks exactly like it. So it
waits half a second to see whether a second click follows.

The click-through window lists one change per row under column headings:

| Column     | Holds                                                              |
| ---------- | ------------------------------------------------------------------ |
| Marker     | Red for a blocking change, amber for a routine one, grey once seen |
| Change     | What happened, such as "Checks broke"                              |
| Repository | The repository it happened in                                      |
| PR         | The pull request number                                            |
| Title      | The pull request's title                                           |
| Who        | Whoever did it: the reviewer, the committer, the commenter         |
| When       | How long ago                                                       |

Who is left blank where GitHub attributes the change to nobody, which is the case for a conflict: it is a consequence of
somebody else's merge into the branch.

It lists the last 20 changes by default, which is a setting. The window has no frame, so drag its title to move it and
its corner mark to resize it. Press Escape, click the close mark, or click anything else on screen to dismiss it.

## What counts as a change

Only transitions raise a notification, never standing state. A pull request that was already failing when the last poll
ran is not reported again, so a large backlog of red pull requests does not become a wall of notifications.

| Change            | Meaning                                          | Notified by default |
| ----------------- | ------------------------------------------------ | ------------------- |
| Review requested  | A pull request joined your review queue          | yes                 |
| Checks broke      | One of yours went from passing to failing        | yes                 |
| Changes requested | A reviewer asked for changes on one of yours     | yes                 |
| Mentioned         | Someone mentioned you or a team you belong to    | yes                 |
| Ready to merge    | Approved, passing, conflict free and not a draft | yes                 |
| Conflict appeared | A pull request now needs a rebase                | no                  |
| New comment       | The comment count went up                        | no                  |

The icon is red when an unread change is blocking someone or something of yours is broken, amber for anything else
unread, green when there is nothing to look at, and grey when the last poll failed.

Notifications are collapsed into one per poll. Clicking one opens the pull request it is about in your default browser,
and where it covers several changes, the one listed first.

## Requirements

- [GitHub CLI](https://cli.github.com/) (`gh`), signed in
- `bash` and `jq`, which the collector script uses. On Windows both ship with Git for Windows
- [uv](https://docs.astral.sh/uv/), which fetches Python and the dependencies
- [gh-dash](https://github.com/dlvhdr/gh-dash) for the dashboard: `gh extension install dlvhdr/gh-dash`

## Running it

Install the dependencies once:

```bash
uv sync
```

Start the tray:

```bash
uv run gh-tray
```

Poll once and print the result, without the tray. This is the quickest way to check the collector and the sign-in:

```bash
uv run gh-tray once
```

Open the settings window on its own:

```bash
uv run gh-tray settings
```

Only one of these can poll at a time. A second tray, or `once` while the tray is running, exits immediately rather than
polling twice. Both would compare against the same stored starting point, so whichever ran first would consume the
changes and the other would never report them. Use the tray's **Refresh now** entry instead.

## Starting at login

Turn on **Start at login** in the tray menu, or **Start automatically at login** in the settings window. This writes a
small launcher for your platform: a hidden-window script in the Startup folder on Windows, a desktop entry under
`~/.config/autostart` on Linux, and a launch agent under `~/Library/LaunchAgents` on macOS. Turning it off removes the
file.

## Settings

The settings window covers the poll interval, which organisations to sweep for newly opened pull requests, how old a
pull request has to be before it is ignored, how many changes the click-through window lists, which changes raise a
notification, whether to start at login, and the paths to the collector, `bash` and the dashboard command. Every path
field may be left blank, in which case the application works it out at runtime.

The organisation list is filled in from your account's memberships the first time the application runs, and the **Detect
organisations** button refills it later.

Settings are re-read on every poll, so a change takes effect without restarting the tray.

## Where things are kept

Settings and history live in the platform's standard application data directory, which
[platformdirs](https://pypi.org/project/platformdirs/) resolves: `%LOCALAPPDATA%\gh-tray` on Windows,
`~/.local/share/gh-tray` on Linux, `~/Library/Application Support/gh-tray` on macOS.

| File             | Contents                                                                    |
| ---------------- | --------------------------------------------------------------------------- |
| `config.json`    | Settings, written by the settings window                                    |
| `state.json`     | The collector's own baseline                                                |
| `snapshot.json`  | Last poll's pull request fields, used to detect the next change             |
| `events.jsonl`   | Changes detected, kept until you have seen them and trimmed to a tail after |
| `seen.json`      | When you last looked, which is what clears the unread count                 |
| `latest.json`    | The last full collector result, written by the collector itself             |
| `summary.json`   | A compact tally of that result, written by the collector itself             |
| `gh-tray.log`    | Rotating diagnostics                                                        |
| `last_error.log` | Everything written when the last collection failed                          |
| `gh-tray.lock`   | Held open while the tray runs, so a second copy cannot start                |

Polling and looking are tracked separately. The poller advances its baseline every run, but the unread count is measured
against the last time you opened the dashboard or chose **Mark all seen**, so nothing is lost between the moment a
change lands and the moment you read it.

Every state file is written to a temporary file and then moved into place. A process stopped part way through a plain
write would leave a truncated file, and a truncated state file is worse than a missing one: it reads as valid but
incomplete, so the change history it describes would be silently wrong.

## How the code is arranged

| Module               | Responsibility                                                            |
| -------------------- | ------------------------------------------------------------------------- |
| `config.py`          | Settings, defaults, repair of hand-edited files, data locations           |
| `storage.py`         | Reading state files, and writing them so a reader never sees half a file  |
| `environment.py`     | Everything platform-specific: bash, terminals, login start, instance lock |
| `collector.py`       | Running the collector script and reading its output                       |
| `events.py`          | Detecting changes between polls, and the event history                    |
| `service.py`         | One polling cycle: collect, diff, record, summarise                       |
| `status.py`          | Turning a poll result into a colour, a count and hover text               |
| `notifier.py`        | Desktop notifications and their click actions                             |
| `tray.py`            | The icon, its menu, and the polling timer                                 |
| `popup.py`           | The frameless window a single click opens                                 |
| `settings_window.py` | The settings window                                                       |

Notifications run on a long-lived event loop of their own. The platform backend calls back into the sending loop when a
notification is clicked, which can happen long after the send returns, so a loop closed straight after sending would
raise on that callback and no click could ever be handled.

## The collector

`src/gh_tray/data/digest.sh` gathers everything in one pass and prints a single JSON document: your open pull requests,
the ones awaiting your review, newly opened pull requests across your organisations, mentions, and check status
transitions. It takes its baseline file as an argument, so it can be pointed at a different one to run without
disturbing this application's.

Its GitHub queries fail and truncate intermittently, which is why a pull request missing from a single result is held
for a few polls before being treated as gone: without that, one transient failure would report everything that came back
as newly arrived.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check .
```
