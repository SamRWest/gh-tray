# gh-tray

A system tray icon that watches your GitHub pull requests, tells you when something changes, and opens a terminal
dashboard when you want the full picture.

The icon carries a count of changes you have not seen yet.

| Doing this to the icon | Gets you                                                                   |
| ---------------------- | -------------------------------------------------------------------------- |
| Hovering               | A short status summary                                                     |
| Clicking once          | A small window listing the most recent changes                             |
| Clicking twice         | [gh-dash](https://github.com/dlvhdr/gh-dash), maximised                    |
| Right-clicking         | The review queue, the login-start switch, the settings window and the rest |

A single click cannot act the moment it happens, because the first click of a double click looks exactly like it. So it
waits half a second to see whether a second click follows.

The window itself appears straight away. It is built once, when the tray starts, and hidden rather than closed
afterwards, so showing it again costs a few milliseconds instead of the second a fresh process takes. It lives in a
process of its own, which the tray starts and stops with itself. Clicking the icon several times in a row leaves one
note asking for it, not several windows.

The click-through window lists what changed since you last looked, then what is merely waiting on you. Changes alone
would leave it saying "nothing" on a quiet day while three reviews sat in the queue.

| Column     | Holds                                                                   |
| ---------- | ----------------------------------------------------------------------- |
| Change     | What happened, or how it stands: "Checks broke", "Awaiting your review" |
| Repository | The repository it is in                                                 |
| PR         | The pull request number                                                 |
| Title      | The pull request's title                                                |
| Who        | Whoever did it: the reviewer, the committer, the commenter              |
| When       | How long ago                                                            |

The window follows your desktop's light or dark theme, as does the settings window. The theme is read when the window
opens, so changing it takes effect the next time you click.

Click a row to open it on GitHub. Right-click it to mark it seen, and right-click again to mark it unseen. Nothing else
marks a row: a row you have marked comes back unmarked if anything happens to it afterwards, since marking means "I have
read this", not "stop telling me about this pull request". **Mark all seen** in the tray menu clears the lot.

Each row keeps the colour of what it is: red when something is blocking, amber when it is worth a look, green when it is
good news such as a pull request that could be merged. A row you have seen is dimmed and its mark goes hollow, and
nothing else dims it. Age has a scale of its own in the date column, running from just-happened to long-forgotten. Who
is left blank where GitHub attributes the change to nobody, which is the case for a conflict: it is a consequence of
somebody else's merge into the branch.

These are the states counted as waiting on you: a pull request in your review queue, one of yours where a reviewer asked
for changes, one of yours whose checks are failing, and one of yours that could be merged as it stands.

One row per pull request: three comments on the same one are one thing to look at, not three. Changes you caused
yourself are left out, since your own comment is not news to you, though your own commit breaking the checks still is.
**Refresh** asks the tray to look again, since it is the only thing allowed to poll.

Newest is at the top. Click a column heading to sort by it and again to turn the order around. It lists 20 rows by
default, which is a setting, and the rest scroll. Drag a divider in the headings to resize a column. The window has no
frame, so drag its title strip to move it and any edge or corner to resize it. Press Escape, click the close mark, or
click anything else on screen to dismiss it.

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

- [uv](https://docs.astral.sh/uv/), which fetches Python and the dependencies
- [GitHub CLI](https://cli.github.com/) (`gh`), signed in
- [gh-dash](https://github.com/dlvhdr/gh-dash) for the dashboard

Nothing else: no shell, and no command line tools beyond the GitHub one. The application never handles a token of its
own. It borrows whatever you have already signed in with, and stops working the moment you sign out.

To see where you stand and install what is missing:

```bash
uv run gh-tray setup
```

Each requirement is listed with a light: 🟢 present, 🟡 missing but installable from here, 🔴 missing and needing you. Only
installs that manage their own elevation are ever run, meaning a package manager that asks for administrator rights
itself, or a GitHub extension that lands in your own directory. Anything needing a root shell is printed for you to run,
because a desktop application quietly acquiring root is not a thing anyone should have to trust. Signing in is never
done for you either, since that means entering credentials.

Starting the tray from a terminal with something missing offers the same thing before it starts. Starting it from a
login entry, where there is nobody to ask, it says what is missing and stops rather than showing an icon that could
never report anything.

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

The settings window covers the poll interval, how old a pull request has to be before it is ignored, how many changes
the click-through window lists, which changes raise a notification, whether to start at login, and the command that
opens the dashboard. Leaving the dashboard command blank runs `gh dash` in whichever terminal this platform provides.

Settings are re-read on every poll, so a change takes effect without restarting the tray.

## Where things are kept

Settings and history live in the platform's standard application data directory, which
[platformdirs](https://pypi.org/project/platformdirs/) resolves: `%LOCALAPPDATA%\gh-tray` on Windows,
`~/.local/share/gh-tray` on Linux, `~/Library/Application Support/gh-tray` on macOS.

| File             | Contents                                                                    |
| ---------------- | --------------------------------------------------------------------------- |
| `config.json`    | Settings, written by the settings window                                    |
| `state.json`     | When the last collection ran, which is the window mentions are asked for    |
| `snapshot.json`  | Last poll's pull request fields, used to detect the next change             |
| `events.jsonl`   | Changes detected, kept until you have seen them and trimmed to a tail after |
| `seen.json`      | The rows you have marked, and when you last marked everything seen          |
| `gh-tray.log`    | Rotating diagnostics                                                        |
| `last_error.log` | Everything written when the last collection failed                          |
| `gh-tray.lock`   | Held open while the tray runs, so a second copy cannot start                |
| `popup.lock`     | Held open by the hidden changes window, so only one waits                   |

Polling and looking are tracked separately. The poller advances its baseline every run, but the unread count is measured
against what you have actually looked at, so nothing is lost between the moment a change lands and the moment you read
it. Right-clicking a row marks that one row; **Mark all seen** sets a single timestamp and anything older than it counts
as seen without a mark of its own.

Every state file is written to a temporary file and then moved into place. A process stopped part way through a plain
write would leave a truncated file, and a truncated state file is worse than a missing one: it reads as valid but
incomplete, so the change history it describes would be silently wrong.

## How the code is arranged

| Module               | Responsibility                                                           |
| -------------------- | ------------------------------------------------------------------------ |
| `config.py`          | Settings, defaults, repair of hand-edited files, data locations          |
| `storage.py`         | Reading state files, and writing them so a reader never sees half a file |
| `environment.py`     | Everything platform-specific: terminals, login start, instance lock, DPI |
| `github.py`          | Talking to GitHub through the signed-in command line tool                |
| `collector.py`       | Asking GitHub what is true now, and flattening the answer                |
| `events.py`          | Detecting changes between polls, and the event history                   |
| `service.py`         | One polling cycle: collect, diff, record, summarise                      |
| `status.py`          | Turning a poll result into a colour, a count and hover text              |
| `notifier.py`        | Desktop notifications and their click actions                            |
| `tray.py`            | The icon, its menu, and the polling timer                                |
| `theme.py`           | The colours to draw with, following the desktop's light or dark theme    |
| `popup.py`           | Which rows the click-through window lists, and in what order             |
| `window.py`          | The frameless window itself, its table, and staying loaded and hidden    |
| `settings_window.py` | The settings window                                                      |

The table is [tksheet](https://github.com/ragardner/tksheet) rather than the toolkit's own, which colours a whole row at
a time. Here each cell wants its own colour: a row says what it is in one and how stale it is in another, and neither
should have to give way to the other.

Notifications run on a long-lived event loop of their own. The platform backend calls back into the sending loop when a
notification is clicked, which can happen long after the send returns, so a loop closed straight after sending would
raise on that callback and no click could ever be handled.

## The collector

One poll makes two searches, one for your open pull requests and one for those awaiting your review, and one read of the
notifications feed. Each search asks for the state of every pull request along with the last commit's author, the most
recent review's author and the most recent comment's author, since those are what name the person behind a change. The
notifications feed identifies a mention only by the comment it points at, so the first few of those are looked up one at
a time.

Collecting knows nothing about what was true last time: it reports only what is true now, and comparing is somebody
else's job. That is what lets a poll fail without disturbing anything already recorded.

GitHub answers a heavy search with an error often enough that retrying is normal rather than exceptional, and an error
arrives as well-formed JSON carrying no results. A reply therefore counts as usable only once it actually holds results,
and each page is retried three times before the collection gives up.

The same unreliability is why a pull request missing from a single result is held for a few polls before being treated
as gone: without that, one transient failure would report everything that came back as newly arrived.

## Development

Set the project up, which installs the dependencies and the pre-commit hooks:

```bash
uv run poe init
```

The jobs are named, and `uv run poe` on its own lists them:

| Job        | Does                                                        |
| ---------- | ----------------------------------------------------------- |
| `test`     | Runs the tests                                              |
| `ty`       | Runs the type checks                                        |
| `lint`     | Runs the pre-commit checks on staged files                  |
| `lint_all` | Runs them on every file, staged or not                      |
| `run`      | Starts the tray                                             |
| `once`     | Polls a single time and prints the result, without the tray |

The checks are formatting and linting with [ruff](https://docs.astral.sh/ruff/), type checking with
[ty](https://github.com/astral-sh/ty), a workflow linter, a scan of the dependencies for known vulnerabilities, and the
usual file hygiene. Ruff's security rules are on, which is the same set [bandit](https://bandit.readthedocs.io/)
implements, so bandit is not installed separately.

Every push runs the tests on Linux and Windows and the checks once, in the **Checks and tests** workflow. Windows is
where the awkward parts live: measuring the taskbar, locking against a second copy, and starting a window with no
console. Linux is checked because the tray, the login entry and the terminal launcher all claim to work there.

Tests that need a window skip themselves where there is no display to build one on.
