"""Summarising a poll result, and drawing it as a tray icon and hover text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .events import BROKEN_CI, is_urgent

RED, AMBER, GREEN, GREY = "#d1242f", "#bf8700", "#1a7f37", "#6e7781"

ICON_SIZE = 64

# The application's own mark, kept in step with data/icon.svg: the sizes Windows asks for, the tray it draws,
# and the three marks landing in it, in the colours the window uses for what wants attention.
APP_ICON_SIZE = 256
APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICON_REFERENCE = 64
ICON_BACKGROUND = "#22272e"
ICON_TRAY = "#adbac7"
ICON_MARKS = ((21, 17, "#ff7b72"), (32, 25, "#ffa657"), (43, 17, "#5ddb6f"))
ICON_TRAY_OUTLINE = ((13, 36), (18, 51), (46, 51), (51, 36))
# Windows caps a tray tooltip near 128 characters of plain text, and offers no way to style it.
TOOLTIP_LIMIT = 127


@dataclass(frozen=True)
class Status:
    """A rendered view of one poll result, used for the tooltip, the icon and the menu header."""

    authored: int = 0
    reviewing: int = 0
    red: int = 0
    pending: int = 0
    polled_at: str = ""
    unread: int = 0
    colour: str = GREY
    error: str = ""


def status_from(digest: dict, unread: list[dict], error: str = "", polled_at: str | None = None) -> Status:
    """Build the status summary shown in the tooltip, the icon and the menu header.

    :param digest: the full collector result, empty when the poll failed
    :param unread: events the user has not seen, which set the count and the colour
    :param error: description of a failed poll, which forces the grey state
    :param polled_at: local time of the poll, defaulting to now
    :return: the summary
    """
    stamp = polled_at if polled_at is not None else datetime.now().strftime("%H:%M")
    if error or not digest:
        return Status(polled_at=stamp, colour=GREY, error=error, unread=len(unread))
    pull_requests = digest.get("authored", []) + digest.get("reviewing", [])
    colour = RED if any(is_urgent(event["kind"]) for event in unread) else AMBER if unread else GREEN
    return Status(
        authored=len(digest.get("authored", [])),
        reviewing=len(digest.get("reviewing", [])),
        red=sum(1 for pull_request in pull_requests if pull_request.get("ci") in BROKEN_CI),
        pending=sum(1 for pull_request in pull_requests if pull_request.get("ci") == "PENDING"),
        polled_at=stamp,
        unread=len(unread),
        colour=colour,
    )


def summary_line(status: Status) -> str:
    """Return the one-line description used as the menu header."""
    if status.error:
        return f"Poll failed: {status.error}"
    return f"{status.reviewing} to review - {status.red} red - {status.authored} open"


def tooltip_text(status: Status, app_name: str = "gh-tray") -> str:
    """Render the hover summary.

    The platform tooltip is plain text with a hard length cap, so the least useful line is dropped rather than the
    text being truncated mid-word.

    :param status: the summary to render
    :param app_name: leading name, shown so the icon is identifiable among other tray icons
    """
    if status.error:
        return f"{app_name} - poll failed\n{status.error}"[:TOOLTIP_LIMIT]
    headline = f"{status.unread} unread change{'s' if status.unread != 1 else ''}" if status.unread else "no changes"
    lines = [
        f"{app_name} - {headline}",
        f"{status.reviewing} awaiting your review",
        f"{status.authored} open, {status.red} red, {status.pending} pending",
        f"polled {status.polled_at}",
    ]
    while len("\n".join(lines)) > TOOLTIP_LIMIT and len(lines) > 2:
        lines.pop()
    return "\n".join(lines)[:TOOLTIP_LIMIT]


def app_icon(size: int = APP_ICON_SIZE) -> Image.Image:
    """Draw the application's own mark: a tray with three things landing in it.

    The same design as ``data/icon.svg``, which is the editable original and the one Linux desktops are given.
    Windows and the notification service want a raster image, and drawing it here avoids carrying a renderer for
    vector graphics along with its native libraries just to produce one small picture.

    :param size: how many pixels square to draw it
    """
    scale = size / ICON_REFERENCE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    canvas.rounded_rectangle((0, 0, size - 1, size - 1), radius=round(14 * scale), fill=ICON_BACKGROUND)
    for centre_x, centre_y, colour in ICON_MARKS:
        radius = 4.5 * scale
        middle = (centre_x * scale, centre_y * scale)
        canvas.ellipse((middle[0] - radius, middle[1] - radius, middle[0] + radius, middle[1] + radius), fill=colour)
    # An open tray rather than a closed box, so it reads as something things land in.
    canvas.line([(point[0] * scale, point[1] * scale) for point in ICON_TRAY_OUTLINE], fill=ICON_TRAY, width=max(1, round(4 * scale)), joint="curve")
    return image


def write_app_icon(path: Path) -> Path:
    """Write the application's mark where the desktop can pick it up, at the sizes Windows asks for.

    :param path: the file to write
    :return: the same path
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    largest = app_icon(max(APP_ICON_SIZES))
    largest.save(path, sizes=[(size, size) for size in APP_ICON_SIZES])
    return path


def build_image(colour: str, count: int) -> Image.Image:
    """Draw the tray icon: a filled disc carrying the unread change count.

    :param colour: fill colour of the disc
    :param count: unread changes, omitted from the icon when zero and shown as ``9+`` above nine
    """
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    canvas.ellipse((1, 1, ICON_SIZE - 2, ICON_SIZE - 2), fill=colour)
    if count:
        label = str(count) if count < 10 else "9+"
        canvas.text(
            (ICON_SIZE / 2, ICON_SIZE / 2 + 1),
            label,
            font=ImageFont.load_default(size=42 if count < 10 else 34),
            fill="white",
            anchor="mm",
        )
    return image
