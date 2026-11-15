"""YouTube ads added by hand: the only source in the library whose video an LLM can watch.

Why by hand and not through the YouTube Data API (checked 15 Nov 2026,
developers.google.com/youtube/terms/developer-policies): the policies forbid using API Data
"to create new or derived data or metrics" (III.E.4.h), limit aggregation to channels of one
content owner (III.E.2.a) and cap storage of statistics at 30 days (III.E.4.b). A competitor
library ranking other companies' videos is exactly that, so the Data API is not used, and no
YouTube view count ever feeds the winner score. youtube.com is not fetched at all (no oEmbed,
no page): a row is the video id the owner pasted, plus what the owner typed.

What a row allows: (1) the official embedded player (www.youtube-nocookie.com/embed/<id>) and
the thumbnail image i.ytimg.com/vi/<id>/hqdefault.jpg on the detail page, attributed to
YouTube (III.F.2.a); (2) tagging by Gemini, which the Vertex AI documentation allows to read a
public YouTube URL directly ("video understanding": fileUri with a youtube.com URL, one video
per request) - nothing is downloaded, cached or stored by this package (III.E.1.a).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}


def video_id(url_or_id: str) -> str | None:
    """A YouTube video id from a watch / shorts / embed / youtu.be URL, or the bare id."""
    s = str(url_or_id or "").strip()
    if _ID.match(s):
        return s
    try:
        p = urlsplit(s)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in _HOSTS:
        return None
    host = (p.hostname or "").lower()
    cand = None
    if host == "youtu.be":
        cand = p.path.strip("/").split("/")[0]
    elif p.path == "/watch":
        cand = (parse_qs(p.query).get("v") or [""])[0]
    else:
        m = re.match(r"^/(?:shorts|embed|live)/([^/?#]+)", p.path)
        cand = m.group(1) if m else None
    return cand if cand and _ID.match(cand) else None


def watch_url(vid: str) -> str | None:
    return f"https://www.youtube.com/watch?v={vid}" if _ID.match(vid or "") else None


def embed_url(vid: str) -> str | None:
    return f"https://www.youtube-nocookie.com/embed/{vid}" if _ID.match(vid or "") else None


def thumb_url(vid: str) -> str | None:
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if _ID.match(vid or "") else None
