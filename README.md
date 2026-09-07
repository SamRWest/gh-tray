# gh-tray

For when you can't keep track of all your pull requests.

A system tray app that watches your GitHub pull requests, lists what changed, sends a desktop notification when
something needs you, and opens the pull request or a terminal dashboard ([gh-dash](https://github.com/dlvhdr/gh-dash)).
Works on Windows, macOS and Linux.

![The changes window, listing what wants attention](docs/popup.png)

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and [git](https://git-scm.com/). Run it straight
from GitHub, with nothing installed:

```bash
uvx --refresh --from git+https://github.com/SamRWest/gh-tray gh-tray
```

Or install it as a tool, which puts `gh-tray` on your path:

```bash
uv tool install --refresh git+https://github.com/SamRWest/gh-tray
```

Remove it again with `uv tool uninstall gh-tray`.

On first start it checks for the [GitHub CLI](https://cli.github.com/) and gh-dash, and offers to install what is
missing. Sign in with `gh auth login` if you have not already; answer HTTPS to the protocol question. You can check
everything again at any time with `gh-tray setup` (with the `uvx` form above, put `setup` on the end).

## Use

The tray icon shows how many changes you have not seen yet. It is red when something of yours is broken or blocked,
amber for anything else unread, green when there is nothing to do, and grey when the last poll failed.

| On the icon  | Gets you                                                           |
| ------------ | ------------------------------------------------------------------ |
| Hover        | A short status summary                                             |
| Click        | The changes window                                                 |
| Right click  | The menu: dashboard, refresh, mark all seen, settings, log, quit   |
| Middle click | The same menu, on desktops that keep the right click to themselves |

In the changes window:

- Double-click a row to open it on GitHub. Right-click a row to mark it seen, or unseen again.
- Click a heading to sort. Use the buttons at the bottom to filter, and Ctrl+F to search every column.
- Hold Ctrl and scroll to change the text size. Drag the title to move the window and an edge to resize it.
- **Open dashboard** opens gh-dash in a terminal. **Menu** opens the tray menu, for desktops that offer no other way.

You are notified when a review is requested of you, your checks break, a reviewer asks for changes, someone mentions
you, or a pull request of yours becomes ready to merge. New comments and merge conflicts are listed but not announced
unless you turn them on. Only changes are reported, never standing state, so an old backlog does not become a wall of
notifications.

## Settings

Open **Settings...** from the menu to set how often to poll, how old a pull request may be before it is ignored, how
many rows the window shows, which changes to be notified about, which repository owners to watch, whether to start at
login, and the dashboard command. **Also list** adds the pull requests you only commented on or were assigned, which is
what gh-dash's Involved section shows. Changes take effect on the next poll.

## If something goes wrong

**Open log** in the menu shows the log. To watch the tray in a terminal instead, run `gh-tray --foreground`, or add
`--verbose` for every detail. Settings and history live in your platform's application data folder:
`%LOCALAPPDATA%\gh-tray` on Windows, `~/.local/share/gh-tray` on Linux, and `~/Library/Application Support/gh-tray` on
macOS.

On Linux the toolkit also needs the `libxcb-cursor0` package (or your distribution's equivalent); `gh-tray setup` names
the command to install it.

## Developing

Clone the repository and run `uv run poe init` to install the dependencies and pre-commit hooks. `uv run poe` lists the
jobs: tests, type checks, lint, and running the tray from source.
