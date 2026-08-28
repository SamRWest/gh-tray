"""A system tray watcher for GitHub pull request activity.

The tray icon carries a count of changes not yet seen, hovering summarises the current state, double-clicking opens a
terminal dashboard, and the right-click menu reaches recent changes, the review queue and the settings window.
"""

__version__ = "0.1.0"

APP_NAME = "gh-tray"
# The name to start this package under, for the windows that run as processes of their own. Taken from the package
# rather than written out, so a rename cannot leave a spelling behind that only fails once a window is opened.
APP_MODULE = __name__
