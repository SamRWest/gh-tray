"""The settings window: polling, notification rules, the dashboard command, login start and GitHub sign-in.

It runs as its own process so its user interface loop never shares a thread with the tray icon's.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from loguru import logger

from . import APP_NAME
from .config import APP_ICON_PATH, TEXT_KEYS, THEME_KEY, load_config, save_config
from .environment import autostart_enabled, github_auth_summary, make_dpi_aware, open_in_terminal, set_autostart
from .events import RULE_LABELS
from .prerequisites import signed_in
from .status import write_app_icon
from .theme import ALWAYS_DARK, ALWAYS_LIGHT, FOLLOW_DESKTOP, PALETTE, blend

NUMBER_FIELDS = ("poll_minutes", "max_age_days", "popup_rows")
FIELDS = {
    "poll_minutes": ("Poll every (minutes)", 8),
    "max_age_days": ("Hide pull requests older than (days, 0 = keep all)", 8),
    "popup_rows": ("Changes shown when you click the tray icon", 8),
    "dashboard_command": ("Dashboard command (blank = gh dash)", 46),
}


POINTS_PER_INCH = 72.0

# The theme choices offered, and what each is called in the window.
THEME_CHOICES = ((FOLLOW_DESKTOP, "Follow the desktop"), (ALWAYS_DARK, "Dark"), (ALWAYS_LIGHT, "Light"))


def apply_theme(root: tk.Tk) -> None:
    """Colour every kind of widget this window uses to match the desktop's theme.

    The theme is switched to one that honours colours first. The default on Windows draws its own and ignores most
    of what is set here, which would leave a white window in a dark desktop.

    :param root: the window to style
    """
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=PALETTE.background)
    style.configure(".", background=PALETTE.background, foreground=PALETTE.text, fieldbackground=PALETTE.surface, borderwidth=0)
    style.configure("TFrame", background=PALETTE.background)
    style.configure("TLabel", background=PALETTE.background, foreground=PALETTE.text)
    style.configure("TCheckbutton", background=PALETTE.background, foreground=PALETTE.text, focuscolor=PALETTE.background)
    style.map(
        "TCheckbutton",
        background=[("active", PALETTE.background)],
        indicatorcolor=[("selected", PALETTE.green), ("!selected", PALETTE.surface)],
    )
    style.configure("TEntry", fieldbackground=PALETTE.surface, foreground=PALETTE.text, insertcolor=PALETTE.text, bordercolor=PALETTE.border)
    style.configure(
        "TButton", background=PALETTE.surface, foreground=PALETTE.text, bordercolor=PALETTE.border, focuscolor=PALETTE.surface, padding=(10, 4)
    )
    style.map("TButton", background=[("active", PALETTE.hover)])
    # The one button that commits stands out from the ones that do not, so the eye lands on it first.
    style.configure("Accent.TButton", background=PALETTE.selection, foreground=PALETTE.heading, focuscolor=PALETTE.selection, padding=(10, 4))
    style.map("Accent.TButton", background=[("active", blend(PALETTE.selection, PALETTE.heading, 0.85))])
    # Section names carry the window's structure, so they read a step heavier than what sits under them.
    style.configure("Heading.TLabel", background=PALETTE.background, foreground=PALETTE.heading, font=("TkDefaultFont", 10, "bold"))
    style.configure("TSeparator", background=PALETTE.border)
    style.configure("TRadiobutton", background=PALETTE.background, foreground=PALETTE.text, focuscolor=PALETTE.background)
    style.map(
        "TRadiobutton",
        background=[("active", PALETTE.background)],
        indicatorcolor=[("selected", PALETTE.green), ("!selected", PALETTE.surface)],
    )


def run_settings() -> None:
    """Show the settings window and block until it is closed."""
    make_dpi_aware()
    config = load_config()
    root = tk.Tk()
    # Points become the right physical size only once Tk knows the real resolution of the screen.
    root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / POINTS_PER_INCH)
    root.title(f"{APP_NAME} settings")
    root.resizable(False, False)
    try:
        # The application's own mark in the title bar, in place of the toolkit's default.
        root.iconbitmap(str(write_app_icon(APP_ICON_PATH)))
    except (tk.TclError, OSError) as error:
        logger.debug("could not put the application's mark on the settings window: {}", error)
    apply_theme(root)
    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")

    entries: dict[str, tk.StringVar] = {}
    row = 0
    for key, (label, width) in FIELDS.items():
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 12))
        variable = tk.StringVar(value=str(config[key]))
        ttk.Entry(frame, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", pady=3)
        entries[key] = variable
        row += 1

    ttk.Separator(frame, orient="horizontal").grid(row=row, columnspan=2, sticky="ew", pady=8)
    row += 1
    ttk.Label(frame, text="Notify me about", style="Heading.TLabel").grid(row=row, column=0, sticky="w", pady=(0, 4))
    row += 1
    toggles: dict[str, tk.BooleanVar] = {}
    for kind, (label, _urgent) in RULE_LABELS.items():
        switch = tk.BooleanVar(value=bool(config["toasts"].get(kind)))
        ttk.Checkbutton(frame, text=label, variable=switch).grid(row=row, column=0, columnspan=2, sticky="w")
        toggles[kind] = switch
        row += 1

    ttk.Separator(frame, orient="horizontal").grid(row=row, columnspan=2, sticky="ew", pady=8)
    row += 1
    ttk.Label(frame, text="Colours", style="Heading.TLabel").grid(row=row, column=0, sticky="w")
    style_choice = tk.StringVar(value=config[THEME_KEY])
    styles = ttk.Frame(frame)
    styles.grid(row=row, column=1, sticky="w")
    for column, (value, label) in enumerate(THEME_CHOICES):
        ttk.Radiobutton(styles, text=label, value=value, variable=style_choice).grid(row=0, column=column, padx=(0, 10))
    row += 1
    ttk.Label(frame, text="Takes effect the next time a window opens.", foreground=PALETTE.muted).grid(row=row, column=1, sticky="w", pady=(0, 4))
    row += 1

    ttk.Separator(frame, orient="horizontal").grid(row=row, columnspan=2, sticky="ew", pady=8)
    row += 1
    autostart = tk.BooleanVar(value=autostart_enabled())
    ttk.Checkbutton(frame, text="Start automatically at login", variable=autostart).grid(row=row, column=0, columnspan=2, sticky="w")
    row += 1
    # Whether you are signed in decides whether anything works at all, so it says so in colour as well as in words.
    ttk.Label(frame, text=github_auth_summary(), wraplength=440, foreground=PALETTE.green if signed_in() else PALETTE.red).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(6, 0)
    )
    row += 1

    def sign_in() -> None:
        """Launch an interactive GitHub sign-in in a terminal window."""
        try:
            open_in_terminal("gh auth login", "gh auth")
        except RuntimeError as error:
            messagebox.showerror(APP_NAME, str(error))

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
        config[THEME_KEY] = style_choice.get()
        save_config(config)
        set_autostart(autostart.get())
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
    for column, (text, command, kind) in enumerate(
        (
            ("Sign in to GitHub", sign_in, "TButton"),
            ("Cancel", root.destroy, "TButton"),
            ("Save", save_and_close, "Accent.TButton"),
        )
    ):
        ttk.Button(buttons, text=text, command=command, style=kind).grid(row=0, column=column, padx=4)
    root.mainloop()
