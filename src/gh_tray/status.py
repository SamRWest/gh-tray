"""Summarising a poll result, and drawing it as a tray icon and hover text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

from .events import BROKEN_CI, is_urgent
from .theme import blend

RED, AMBER, GREEN, GREY = "#d1242f", "#bf8700", "#1a7f37", "#6e7781"

ICON_SIZE = 64

# The application's own mark, kept in step with data/icon.svg: three coloured dots reading as three rows of a
# list, each with the row it belongs to beside it. Drawn on a grid ICON_REFERENCE units square and scaled up.
APP_ICON_SIZE = 256
# The sizes desktops show the mark at, from a tray tile to a notification banner. The file holds the largest and is
# scaled down from there, so the drawing is checked at each of these rather than written at each.
APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICON_REFERENCE = 64
ICON_CORNER = 16
# The field the mark sits on, which fades down the square rather than sitting flat.
ICON_TOP = "#2e343d"
ICON_BOTTOM = "#1e232a"
# Each row: how far down it sits, how long its bar is, and its colour, being the three the window uses for what
# wants attention.
ICON_ROWS = ((19, 22, "#ff7b72"), (32, 15, "#ffa657"), (45, 19, "#5ddb6f"))
ICON_DOT_X = 18
ICON_DOT_RADIUS = 5
ICON_BAR_X = 28
ICON_BAR_HEIGHT = 7
# How strongly the bars are drawn against the field. They are texture rather than detail: at the smallest sizes
# they melt into a soft block and the three dots carry the mark alone.
ICON_BAR_STRENGTH = 0.34
# Drawn this many times larger and then reduced. The drawing has no smoothing of its own, and a sixteen pixel icon
# of hard-edged circles is a mess of steps.
ICON_OVERSAMPLE = 4
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


def fading_field(size: int) -> Image.Image:
    """Draw the square the mark sits on, fading from the lighter colour at the top to the darker at the bottom.

    :param size: how many pixels square to draw it
    """
    column = Image.new("RGB", (1, size))
    for row in range(size):
        column.putpixel((0, row), ImageColor.getrgb(blend(ICON_TOP, ICON_BOTTOM, 1.0 - row / max(1, size - 1))))
    return column.resize((size, size))


def app_icon(size: int = APP_ICON_SIZE) -> Image.Image:
    """Draw the application's own mark: three coloured dots as three rows of a list.

    The same design as ``data/icon.svg``, which is the editable original. The desktops and the notification
    service want a raster image, and drawing it here avoids carrying a renderer for
    vector graphics along with its native libraries just to produce one small picture.

    :param size: how many pixels square to draw it
    """
    drawn = size * ICON_OVERSAMPLE
    scale = drawn / ICON_REFERENCE
    corners = Image.new("L", (drawn, drawn), 0)
    ImageDraw.Draw(corners).rounded_rectangle((0, 0, drawn - 1, drawn - 1), radius=round(ICON_CORNER * scale), fill=255)
    image = Image.new("RGBA", (drawn, drawn), (0, 0, 0, 0))
    image.paste(fading_field(drawn), mask=corners)
    canvas = ImageDraw.Draw(image)
    for middle, bar_width, colour in ICON_ROWS:
        radius = ICON_DOT_RADIUS * scale
        centre = (ICON_DOT_X * scale, middle * scale)
        canvas.ellipse((centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius), fill=colour)
        half = ICON_BAR_HEIGHT * scale / 2
        bar = (ICON_BAR_X * scale, centre[1] - half, (ICON_BAR_X + bar_width) * scale, centre[1] + half)
        # Mixed against the field at this row's own height, since the field is lighter at the top than the bottom.
        behind = blend(ICON_TOP, ICON_BOTTOM, 1.0 - middle / ICON_REFERENCE)
        canvas.rounded_rectangle(bar, radius=half, fill=blend(colour, behind, ICON_BAR_STRENGTH))
    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_app_icon(path: Path) -> Path:
    """Write the application's mark where the desktop can pick it up.

    A portable picture rather than a Windows icon file: the toolkit puts one in a title bar on every platform and
    every notification service takes one, whereas the icon file is refused outside Windows.

    :param path: the file to write
    :return: the same path
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    app_icon(APP_ICON_SIZE).save(path, format="PNG")
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
