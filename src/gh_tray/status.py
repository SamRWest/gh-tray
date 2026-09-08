"""Summarising a poll result, and drawing it as a tray icon and hover text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from gh_tray.events import BROKEN_CI, is_urgent

if TYPE_CHECKING:
    from PySide6.QtGui import QImage

RED, AMBER, GREEN, GREY = "#d1242f", "#bf8700", "#1a7f37", "#6e7781"

ICON_SIZE = 64

APP_ICON_SIZE = 256
# The application's mark, drawn once as a vector picture and rendered from there at whatever size is asked for.
APP_ICON_SVG = files("gh_tray").joinpath("data", "icon.svg")
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


def app_icon(size: int = APP_ICON_SIZE) -> QImage:
    """Render the application's mark from ``data/icon.svg`` at one size, on a see-through ground.

    :param size: how many pixels square to draw it
    """
    # Imported here rather than at the top, so a windowless command asking for a summary is not forced to load the
    # toolkit. The vector renderer itself needs no running application.
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(APP_ICON_SVG.read_bytes())).render(painter)
    painter.end()
    return image


def write_app_icon(path: Path) -> Path:
    """Write the application's mark where the desktop can pick it up.

    A portable picture, not a Windows icon file, since every platform and notification service accepts one.

    :param path: the file to write
    :return: the same path
    """
    if path.suffix.lower() != ".png":
        raise ValueError(f"the application's mark is written as a PNG, so the path needs that suffix: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # The picture format follows the suffix; naming it as well trips the toolkit's own argument check.
    if not app_icon(APP_ICON_SIZE).save(str(path)):
        raise OSError(f"could not write the application's mark to {path}")
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
