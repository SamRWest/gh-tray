"""The settings window: polling, organisations, notification rules, paths, login start and GitHub sign-in.

It runs as its own process so its user interface loop never shares a thread with the tray icon's.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from . import APP_NAME
from .config import TEXT_KEYS, bootstrap, save_config
from .environment import autostart_enabled, detect_orgs, github_auth_summary, open_in_terminal, set_autostart
from .events import RULE_LABELS

NUMBER_FIELDS = ("poll_minutes", "max_age_days", "popup_rows")
FIELDS = {
    "poll_minutes": ("Poll every (minutes)", 8),
    "orgs": ("Organisations (comma separated, blank = all of yours)", 46),
    "max_age_days": ("Hide pull requests older than (days, 0 = keep all)", 8),
    "popup_rows": ("Changes shown when you click the tray icon", 8),
    "collector": ("Collector script (blank = the one shipped with this app)", 46),
    "bash_path": ("Bash path (blank = auto-detect)", 46),
    "dashboard_command": ("Dashboard command (blank = gh dash)", 46),
}


def run_settings() -> None:
    """Show the settings window and block until it is closed."""
    config = bootstrap()
    root = tk.Tk()
    root.title(f"{APP_NAME} settings")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=12)
    frame.grid(sticky="nsew")

    entries: dict[str, tk.StringVar] = {}
    row = 0
    for key, (label, width) in FIELDS.items():
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        variable = tk.StringVar(value=str(config[key]))
        ttk.Entry(frame, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", pady=2)
        entries[key] = variable
        row += 1

    ttk.Separator(frame, orient="horizontal").grid(row=row, columnspan=2, sticky="ew", pady=8)
    row += 1
    ttk.Label(frame, text="Notify me about").grid(row=row, column=0, sticky="w")
    row += 1
    toggles: dict[str, tk.BooleanVar] = {}
    for kind, (label, _urgent) in RULE_LABELS.items():
        variable = tk.BooleanVar(value=bool(config["toasts"].get(kind)))
        ttk.Checkbutton(frame, text=label, variable=variable).grid(row=row, column=0, columnspan=2, sticky="w")
        toggles[kind] = variable
        row += 1

    ttk.Separator(frame, orient="horizontal").grid(row=row, columnspan=2, sticky="ew", pady=8)
    row += 1
    autostart = tk.BooleanVar(value=autostart_enabled())
    ttk.Checkbutton(frame, text="Start automatically at login", variable=autostart).grid(row=row, column=0, columnspan=2, sticky="w")
    row += 1
    ttk.Label(frame, text=github_auth_summary(), wraplength=440).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
    row += 1

    def sign_in() -> None:
        """Launch an interactive GitHub sign-in in a terminal window."""
        try:
            open_in_terminal("gh auth login", "gh auth")
        except RuntimeError as error:
            messagebox.showerror(APP_NAME, str(error))

    def detect_and_fill() -> None:
        """Fill the organisation field from the account's memberships."""
        found = detect_orgs()
        if found:
            entries["orgs"].set(found)
        else:
            messagebox.showinfo(APP_NAME, "No organisations found. Check that the GitHub CLI is installed and signed in.")

    def save_and_close() -> None:
        """Validate the numeric fields, persist the settings and close."""
        for key in NUMBER_FIELDS:
            if not entries[key].get().strip().isdigit():
                messagebox.showerror(APP_NAME, "Poll interval and age cutoff must be whole numbers.")
                return
            config[key] = int(entries[key].get().strip())
        for key in TEXT_KEYS:
            config[key] = entries[key].get().strip()
        config["toasts"] = {kind: variable.get() for kind, variable in toggles.items()}
        save_config(config)
        set_autostart(autostart.get())
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
    for column, (text, command) in enumerate(
        (
            ("Detect organisations", detect_and_fill),
            ("Sign in to GitHub", sign_in),
            ("Cancel", root.destroy),
            ("Save", save_and_close),
        )
    ):
        ttk.Button(buttons, text=text, command=command).grid(row=0, column=column, padx=4)
    root.mainloop()
