"""Jinja2 templates and filters shared by the browser modules."""

import re
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

PREFIX_RE = re.compile(r"(/[A-Za-z0-9_-]+)+")


def proxy_prefix(value: str) -> str:
    prefix = (value or "").rstrip("/")
    return prefix if PREFIX_RE.fullmatch(prefix) else ""


def proxy_of(request) -> str:
    """Public path prefix when served behind the reverse proxy ('/photos'), else ''.
    The proxy strips the prefix and names it in X-Forwarded-Prefix."""
    return proxy_prefix(request.headers.get("x-forwarded-prefix", ""))


def root_of(request) -> str:
    """What every URL the pages emit starts with ({{ root }}): the proxy prefix plus
    the current library, e.g. '/photos/lib/1a2b3c4d'."""
    lib = request.scope.get("photo_library")
    return proxy_of(request) + (f"/lib/{lib.id}" if lib else "")


def page_context(request) -> dict:
    import photo_library
    return {"root": root_of(request), "proxy": proxy_of(request),
            "library": request.scope.get("photo_library"), "libraries": photo_library.all_libraries()}


def local_path(request, url: str) -> str:
    """Path of a public URL (e.g. HX-Current-URL) as the app's routes see it."""
    path, root = urllib.parse.urlparse(url or "").path, root_of(request)
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root):] or "/"
    return path


templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates",
                            context_processors=[page_context])


def human_size(n: int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(s: float | None) -> str:
    if not s:
        return ""
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def nice_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime("%a %-d %b %Y, %H:%M")


def format_field(label: str, value) -> str:
    """Pretty-print numeric values from the basic extractor (exiftool already formats them)."""
    if isinstance(value, (int, float)):
        if label == "Exposure":
            return f"1/{round(1 / value)}" if 0 < value < 1 else f"{value:g}s"
        if label == "Aperture":
            return f"f/{value:g}"
        if label == "Focal length":
            return f"{value:g} mm"
        if label == "Exp. comp.":
            return f"{value:+g} EV"
    return str(value)


def qs(**params) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v})


templates.env.filters.update(size=human_size, duration=human_duration, nicedate=nice_date)
templates.env.globals.update(qs=qs)


