"""Apple: the legacy iTunes RSS charts (the only Apple feed with game and genre lists) and the
iTunes Lookup API (batched ids, bundleId, a publisher's catalogue).

The legacy `newfreeapplications` feed ignores both `limit` (returns ~107 rows) and `genre`
(on 12 Nov 2026 only 22 of its 107 rows were games); rows beyond the requested length and
non-game rows of a games list are kept raw but left out of scoring (stg_mr_chart).
The newer RSS (rss.marketingtools.apple.com/api/v2) lists only the Apps tab: no games, the
genre filter is ignored, at most 50 rows (and its games endpoint answers 404), so it is not used.
The legacy feed is undocumented: if it breaks, the radar's freshness checks say so.
Rating counts are per storefront and Lookup answers are cached by Apple for up to 24 hours.
A publisher's catalogue is read with `sort=recent`: Lookup returns at most 200 apps, and without
it a large catalogue's 200 are not the newest (16 Nov 2026: one publisher's unsorted answer left
out 3 of its 8 apps released in 2026).
App icons and screenshots are kept only as https URLs on Apple's image CDN (`image_url`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

from .http import APPLE_IMAGE_HOSTS, Fetcher

RSS_URL = "https://itunes.apple.com/{cc}/rss/{chart}/limit={limit}/genre={genre}/json"
LOOKUP_URL = "https://itunes.apple.com/lookup"
CHARTS = (
    "topfreeapplications",
    "toppaidapplications",
    "topgrossingapplications",
    "newfreeapplications",
)
GENRES = {
    "6014": "Games",
    "7001": "Action",
    "7002": "Adventure",
    "7003": "Casual",
    "7009": "Family",
    "7012": "Puzzle",
    "7013": "Racing",
    "7014": "Role Playing",
    "7015": "Simulation",
    "7017": "Strategy",
}
_DEV_ID = re.compile(r"/id(\d+)")


def _label(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _date(s: Any) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(dt.UTC).replace(tzinfo=None)
    return t


def chart_url(cc: str, chart: str, genre: str, limit: int) -> str:
    if chart not in CHARTS:
        raise ValueError(f"unknown chart {chart!r} (known: {', '.join(CHARTS)})")
    return RSS_URL.format(cc=cc.lower(), chart=chart, limit=int(limit), genre=genre)


def parse_chart(
    payload: bytes,
    *,
    snapshot_date: dt.date,
    country: str,
    chart: str,
    genre: str,
    limit: int,
    url: str,
    fetched_at: dt.datetime,
) -> list[dict[str, Any]]:
    doc = json.loads(payload.decode("utf-8"))
    feed = doc.get("feed") or {}
    entries = feed.get("entry") or []
    if isinstance(entries, dict):  # a one-row feed is an object, not a list
        entries = [entries]
    sha = hashlib.sha256(payload).hexdigest()
    updated = _utc(_label(feed, "updated", "label"))
    rows = []
    for i, e in enumerate(entries, start=1):
        attrs = _label(e, "id", "attributes") or {}
        artist_href = _label(e, "im:artist", "attributes", "href") or ""
        m = _DEV_ID.search(artist_href)
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "fetched_at": fetched_at,
                "store": "apple",
                "country": country.lower(),
                "chart": chart,
                "genre_id": str(genre),
                "requested_limit": int(limit),
                "n_rows": len(entries),
                "rank": i,
                "app_id": attrs.get("im:id"),
                "bundle_id": attrs.get("im:bundleId"),
                "name": _label(e, "im:name", "label"),
                "developer": _label(e, "im:artist", "label"),
                "developer_id": m.group(1) if m else None,
                "release_date_feed": _date(_label(e, "im:releaseDate", "label")),
                "category_id": _label(e, "category", "attributes", "im:id"),
                "feed_updated": updated,
                "source_url": url,
                "payload_sha256": sha,
            }
        )
    return rows


def fetch_chart(
    f: Fetcher, snapshot_date: dt.date, cc: str, chart: str, genre: str, limit: int
) -> list[dict[str, Any]]:
    url = chart_url(cc, chart, genre, limit)
    res = f.get(url, f"rss:{cc}:{chart}:{genre}")
    if res.status != 200:
        raise RuntimeError(f"{url} answered {res.status}")
    return parse_chart(
        res.content,
        snapshot_date=snapshot_date,
        country=cc,
        chart=chart,
        genre=genre,
        limit=limit,
        url=url,
        fetched_at=dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0),
    )


def image_url(u: Any) -> str | None:
    """`u` if it is an https URL on Apple's image CDN (is1..is5-ssl.mzstatic.com), else None.

    The host must be exactly one of those names (no port, no user info), and characters that
    would need escaping in HTML or CSS are refused, so a stored URL can go into an <img> as is."""
    if not isinstance(u, str) or not u or len(u) > 1000:
        return None
    if any(c in u for c in " \"'<>\\`(){}") or any(ord(c) < 33 or ord(c) == 127 for c in u):
        return None
    try:
        p = urlsplit(u)
    except ValueError:
        return None
    if p.scheme != "https" or p.netloc not in APPLE_IMAGE_HOSTS or not p.path.startswith("/"):
        return None
    return u


def icon_url(r: dict[str, Any]) -> str | None:
    """The app icon from a Lookup result: artworkUrl512, else artworkUrl100 (Apple CDN only)."""
    return image_url(r.get("artworkUrl512")) or image_url(r.get("artworkUrl100"))


def lookup_url(**query: Any) -> str:
    return f"{LOOKUP_URL}?{urlencode(query, safe=',')}"


def meta_sha(name: Any, description: Any, genres: Any, tags: Any = None) -> str:
    blob = json.dumps([name, description, genres, tags], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def app_row(r: dict[str, Any], snapshot_date: dt.date, cc: str, source: str) -> dict[str, Any]:
    genres = r.get("genres") or []
    return {
        "snapshot_date": snapshot_date,
        "fetched_at": dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0),
        "store": "apple",
        "country": cc.lower(),
        "app_id": str(r.get("trackId")),
        "bundle_id": r.get("bundleId"),
        "name": r.get("trackName"),
        "developer": r.get("sellerName") or r.get("artistName"),
        "developer_id": str(r["artistId"]) if r.get("artistId") is not None else None,
        "release_date": _date(r.get("releaseDate")),
        "current_version_date": _date(r.get("currentVersionReleaseDate")),
        "version": r.get("version"),
        "primary_genre": r.get("primaryGenreName"),
        "genres": json.dumps(genres, ensure_ascii=False),
        "store_tags": None,
        "rating_count": int(r["userRatingCount"]) if r.get("userRatingCount") is not None else None,
        "rating_avg": float(r["averageUserRating"])
        if r.get("averageUserRating") is not None
        else None,
        "rating_count_cur_ver": int(r["userRatingCountForCurrentVersion"])
        if r.get("userRatingCountForCurrentVersion") is not None
        else None,
        "price": float(r["price"]) if r.get("price") is not None else None,
        "currency": r.get("currency"),
        "description": r.get("description"),
        "release_notes": r.get("releaseNotes"),
        "screenshot_urls": json.dumps((r.get("screenshotUrls") or [])[:6]),
        "icon_url": icon_url(r),
        "store_url": (r.get("trackViewUrl") or "").split("?")[0] or None,
        "source": source,
        "meta_sha256": meta_sha(r.get("trackName"), r.get("description"), genres),
    }


def _software(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        x
        for x in doc.get("results") or []
        if x.get("wrapperType") in (None, "software") and x.get("trackId")
    ]


def lookup_ids(
    f: Fetcher,
    ids: list[str],
    cc: str,
    *,
    batch: int = 100,
    purpose: str | None = None,
    cacheable: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Lookup in batches; returns (software results, ids Apple did not return).

    A recheck passes cacheable=False: it must reach Apple again, not the day cache."""
    out: list[dict[str, Any]] = []
    uniq = list(dict.fromkeys(str(i) for i in ids))
    for i in range(0, len(uniq), batch):
        chunk = uniq[i : i + batch]
        res = f.get(
            lookup_url(id=",".join(chunk), country=cc.lower()),
            purpose or f"lookup:{cc}",
            cacheable=cacheable,
        )
        if res.status != 200:
            raise RuntimeError(f"lookup answered {res.status}")
        out += _software(res.json())
    got = {str(x["trackId"]) for x in out}
    return out, [i for i in uniq if i not in got]


def lookup_bundle(f: Fetcher, bundle_id: str, cc: str) -> dict[str, Any] | None:
    res = f.get(lookup_url(bundleId=bundle_id, country=cc.lower()), f"lookup_bundle:{cc}")
    if res.status != 200:
        return None
    apps = _software(res.json())
    return apps[0] if apps else None


def lookup_publisher(f: Fetcher, artist_id: str, cc: str) -> list[dict[str, Any]]:
    """A publisher's newest apps (at most 200) in storefront `cc`."""
    url = lookup_url(
        id=str(artist_id), entity="software", country=cc.lower(), limit=200, sort="recent"
    )
    res = f.get(url, f"publisher:{cc}")
    if res.status != 200:
        raise RuntimeError(f"publisher lookup answered {res.status}")
    return _software(res.json())
