# gh-tray

For when you can't keep track of all your PRs.

A native system tray app that watches your GitHub pull requests, shows a popup table and desktop notification when something changes, and opens the github
page or a terminal dashboard (using [gh-dash](https://github.com/dlvhdr/gh-dash)).

Tested on Windows, Linux and Mac.

![The changes window, listing what wants attention](docs/popup.png)

## Quickstart
You only need [uv](https://docs.astral.sh/uv/getting-started/installation/) and [git](https://git-scm.com/) installed to get started.

Then you can either run it straight from GitHub, with nothing permanently installed:

```bash
uvx --from git+https://github.com/SamRWest/gh-tray gh-tray
```

Or install it as a permanent system tool, which puts `gh-tray` on your path:

```bash
uv tool install git+https://github.com/SamRWest/gh-tray
```

And uninstall using:

```bash
uv tool uninstall gh-tray
```

Either way you need [uv](https://docs.astral.sh/uv/) and the signed-in GitHub CLI; the [Requirements](#requirements)
section has the details, and `gh-tray setup` checks them for you.

The tray icon shows a count of changes you have not seen yet.

| Doing this to the icon | Gets you                                                                   |
|------------------------|----------------------------------------------------------------------------|
| Hovering               | A short status summary                                                     |
| Clicking once          | Shows or hides the changes window, listing the most recent changes         |
| Right-clicking         | The review queue, the login-start switch, the settings window and the rest |
| Middle-clicking        | The same menu, for a desktop that keeps the right click to itself          |


The changes window lists what changed since you last looked, then what is merely waiting on you. Changes alone would
leave it saying "nothing" on a quiet day while three reviews sat in the queue.

| Column | Holds                                                                    |
|--------|--------------------------------------------------------------------------|
| Change | What happened: "Checks broke", "Awaiting your review"  |
| Org    | Who owns the repository                                                  |
| Repo   | The repository it is in                                                  |
| PR     | The pull request number                                                  |
| Status | How it stands right now: open, draft, ready, conflict, merged, or closed |
| Title  | The pull request's title                                                 |
| Author | Whose pull request it is                                                 |
| Who    | Whoever triggered the change: the reviewer, the committer, the commenter |
| When   | How long ago                                                             |

## What counts as a change

Only transitions raise a notification, never standing state. A pull request that was already failing when the last poll
ran is not reported again, so a large backlog of red pull requests does not become a wall of notifications.

| Change            | Meaning                                                                  | Notified by default |
|-------------------|--------------------------------------------------------------------------|---------------------|
| Review requested  | A pull request joined your review queue                                  | yes                 |
| Checks broke      | One of yours went from passing to failing                                | yes                 |
| Changes requested | A reviewer asked for changes on one of yours                             | yes                 |
| Mentioned         | Someone mentioned you or a team you belong to                            | yes                 |
| Ready to merge    | Approved, passing, conflict free and not a draft                         | yes                 |
| Conflict appeared | One of yours now needs a rebase                                          | no                  |
| New comment       | Someone commented on one of yours, or answered a review comment of yours | no                  |

The icon is red when an unread change is blocking someone or something of yours is broken, amber for anything else
unread, green when there is nothing to look at, and grey when the last poll failed.

Notifications are collapsed into one per poll. Clicking one opens the pull request it is about in your default browser,
and where it covers several changes, the one listed first.

## Requirements

- [uv](https://docs.astral.sh/uv/), which fetches Python and the dependencies
- [GitHub CLI](https://cli.github.com/) (`gh`), signed in with `gh auth login`. The Git protocol it asks about makes no
  difference here, since this talks to GitHub through the CLI's own token; HTTPS is the answer with fewer questions.
- [gh-dash](https://github.com/dlvhdr/gh-dash) for the dashboard

You'll be prompted to install anything missing on first start or you can run this to check:

```bash
uv run gh-tray setup
```


## Clone and Run

If you want to run it from source instead of via `uv`, do this:

Install the dependencies once:

```bash
uv sync
```

Start the tray:

```bash
uv run gh-tray
```

Run the process and block the terminal.  Handy for quick troubleshooting without opening log files:

```bash
uv run gh-tray --foreground
```

You can also poll once and read the result:

```bash
uv run gh-tray once --verbose
```

Or open the settings window on its own:

```bash
uv run gh-tray settings
```

## Settings

The settings window covers the poll interval, how old a pull request has to be before it is ignored, how many changes
the changes window lists, which changes raise a notification, which owners' repositories to watch, whether to start at
login, and the command that opens the dashboard. Leaving the dashboard command blank runs `gh dash` in whichever
terminal this platform provides.

Your own account and every organisation it belongs to are listed and on. Turn one off and pull requests and mentions in
its repositories are left out. The first switch, **Any other owner not listed here**, covers every owner not listed,
such as a repository you contribute to from outside your organisations; on, which it is to begin with, an organisation
you join later is watched without a visit to the settings, and off, only the owners ticked are.

**Also list** adds the pull requests you only commented on or were assigned, which is what the dashboard's Involved
section shows. It is off to begin with, so the window lists only what you wrote, what awaits your review and mentions of
you, and can show fewer pull requests than the dashboard. Being involved raises no notification of its own; a reply to a
comment of yours still does.

Settings are re-read on every poll, so a change takes effect without restarting the tray.

## Where things are kept

Settings and history live in the platform's standard application data directory, which
[platformdirs](https://pypi.org/project/platformdirs/) resolves: `%LOCALAPPDATA%\gh-tray` on Windows,
`~/.local/share/gh-tray` on Linux, `~/Library/Application Support/gh-tray` on macOS.

| File                 | Contents                                                                      |
|----------------------|-------------------------------------------------------------------------------|
| `config.json`        | Settings, written by the settings window                                      |
| `state.json`         | When the last collection ran, which is the window mentions are asked for      |
| `snapshot.json`      | Last poll's pull request fields, used to detect the next change               |
| `events.jsonl`       | Changes detected, kept until you have seen them and trimmed to a tail after   |
| `seen.json`          | The rows you have marked, and when you last marked everything seen            |
| `gh-tray.log`        | Rotating diagnostics                                                          |
| `last_error.log`     | Everything written when the last collection failed                            |
| `gh-tray.stderr.log` | Whatever a tray started on its own wrote to its error stream, such as a crash |
| `layout.ini`         | The window width and column widths you last dragged                           |
| `gh-tray.lock`       | Held open while the tray runs, so a second copy cannot start                  |

Polling and looking are tracked separately. The poller advances its baseline every run, but the unread count is measured
against what you have actually looked at, so nothing is lost between the moment a change lands and the moment you read
it. Right-clicking a row marks that one row; **Mark all seen** sets a single timestamp and anything older than it counts
as seen without a mark of its own.

Every state file is written to a temporary file and then moved into place. A process stopped part way through a plain
write would leave a truncated file, and a truncated state file is worse than a missing one: it reads as valid but
incomplete, so the change history it describes would be silently wrong.

## How the code is arranged

| Module               | Responsibility                                                                         |
|----------------------|----------------------------------------------------------------------------------------|
| `config.py`          | Settings, defaults, repair of hand-edited files, data locations                        |
| `storage.py`         | Reading state files, and writing them so a reader never sees half a file               |
| `environment.py`     | Everything platform-specific: terminals, login start, instance lock, Dock, AppleScript |
| `github.py`          | Talking to GitHub through the signed-in command line tool                              |
| `collector.py`       | Asking GitHub what is true now, and flattening the answer                              |
| `events.py`          | Detecting changes between polls, and the event history                                 |
| `service.py`         | One polling cycle: collect, diff, record, summarise                                    |
| `status.py`          | Turning a poll result into a colour, a count and hover text                            |
| `notifier.py`        | Desktop notifications and their click actions                                          |
| `toolkit.py`         | Starting the toolkit, an icon from a drawn picture, the theme setting, the text zoom   |
| `tray.py`            | The icon, its menu, and the polling timer                                              |
| `theme.py`           | The row inks, in a dark and a light set, following the desktop's theme                 |
| `popup.py`           | Which rows the changes window lists, in what order and in which inks                   |
| `window.py`          | The changes window itself and its table                                                |
| `settings_window.py` | The settings window                                                                    |

The table is the toolkit's own, coloured cell by cell.

Notifications run on a long-lived event loop of their own. The platform backend calls back into the sending loop when a
notification is clicked, which can happen long after the send returns, so a loop closed straight after sending would
raise on that callback and no click could ever be handled.

## Development

Set the project up, which installs the dependencies and the pre-commit hooks:

```bash
uv run poe init
```

The jobs are named, and `uv run poe` on its own lists them:

| Job        | Does                                                        |
|------------|-------------------------------------------------------------|
| `test`     | Runs the tests                                              |
| `ty`       | Runs the type checks                                        |
| `ty_all`   | Runs the type checks as Windows, macOS and Linux in turn    |
| `lint`     | Runs the pre-commit checks on staged files                  |
| `lint_all` | Runs them on every file, staged or not                      |
| `run`      | Starts the tray                                             |
| `once`     | Polls a single time and prints the result, without the tray |
