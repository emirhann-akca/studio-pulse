"""Google Play: app details pages only, robots-checked, mapped from the iOS bundleId.

Play has no catalogue API. Its robots.txt allows `/store/apps/details` and disallows the
batchexecute endpoint (`/_/`, used by top-chart, review and permission scrapers) and
`/store/search`; this module only ever builds details URLs and Fetcher.get refuses anything
robots.txt disallows. The page is parsed with JoMingyu/google-play-scraper's `parse_dom`
(MIT, pinned 1.2.7) on HTML we fetched ourselves, so spacing, budget and robots rules stay
ours. The raw page is not stored; only parsed fields are, without the developer's e-mail,
address, website or privacy-policy URL.

`realInstalls` is an undocumented page field: the radar labels it store data ("MAĞAZA
VERİSİ"), never an estimate, and shows the public bucket ("500,000+") next to it.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from .apple import meta_sha
from .http import Fetcher

DETAILS_URL = "https://play.google.com/store/apps/details"
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


class PlayParserMissing(RuntimeError):
    pass


def details_url(package: str, country: str = "us", lang: str = "en") -> str:
    return f"{DETAILS_URL}?{urlencode({'id': package, 'hl': lang, 'gl': country.lower()})}"


def default_parser() -> Callable[[str, str, str], dict[str, Any]]:
    try:
        from google_play_scraper.features.app import parse_dom
    except ImportError as exc:
        raise PlayParserMissing(
            "Play details need google-play-scraper==1.2.7 (pip install 'studio-pulse[radar]')"
        ) from exc
    return lambda html, app_id, url: parse_dom(dom=html, app_id=app_id, url=url)


def parse_released(s: Any) -> dt.date | None:
    """'Oct 10, 2026' (hl=en) -> date, without the process locale."""
    if not s or not isinstance(s, str):
        return None
    parts = s.replace(",", " ").split()
    if len(parts) != 3:
        return None
    month = _MONTHS.get(parts[0][:3].lower())
    try:
        return dt.date(int(parts[2]), month, int(parts[1])) if month else None
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def app_row(d: dict[str, Any], snapshot_date: dt.date, country: str) -> dict[str, Any]:
    tags = [c.get("name") for c in d.get("categories") or [] if isinstance(c, dict)]
    desc = d.get("description")
    return {
        "snapshot_date": snapshot_date,
        "fetched_at": dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0),
        "store": "play",
        "country": country.lower(),
        "app_id": d.get("appId"),
        "bundle_id": d.get("appId"),
        "name": d.get("title"),
        "developer": d.get("developer"),
        "developer_id": None,
        "release_date": parse_released(d.get("released")),
        "current_version_date": dt.datetime.fromtimestamp(int(d["updated"]), dt.UTC).date()
        if isinstance(d.get("updated"), (int, float))
        else None,
        "version": d.get("version"),
        "primary_genre": d.get("genre"),
        "genres": json.dumps([d.get("genreId")] if d.get("genreId") else []),
        "store_tags": json.dumps(tags, ensure_ascii=False),
        "rating_count": _int(d.get("ratings")),
        "rating_avg": float(d["score"]) if isinstance(d.get("score"), (int, float)) else None,
        "rating_count_cur_ver": None,
        "installs_bucket": d.get("installs"),
        "min_installs": _int(d.get("minInstalls")),
        "real_installs": _int(d.get("realInstalls")),
        "histogram": json.dumps(d.get("histogram")) if d.get("histogram") else None,
        "price": float(d["price"]) if isinstance(d.get("price"), (int, float)) else None,
        "currency": d.get("currency"),
        "offers_iap": bool(d.get("offersIAP")) if d.get("offersIAP") is not None else None,
        "iap_range": d.get("inAppProductPrice"),
        "contains_ads": bool(d.get("containsAds")) if d.get("containsAds") is not None else None,
        "description": desc,
        "release_notes": None,
        "summary": d.get("summary"),
        "screenshot_urls": json.dumps((d.get("screenshots") or [])[:6]),
        "video_url": d.get("video"),
        "store_url": details_url(d.get("appId") or "", country).split("&")[0],
        "source": "play_details",
        "meta_sha256": meta_sha(d.get("title"), desc, d.get("genreId"), tags),
    }


def fetch_details(
    f: Fetcher,
    package: str,
    country: str = "us",
    lang: str = "en",
    parser: Callable[[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Parsed details, or None when Play has no such package (404)."""
    url = details_url(package, country, lang)
    res = f.get(url, "play_details", cacheable=False)
    if res.status == 404:
        return None
    if res.status != 200:
        raise RuntimeError(f"Play details answered {res.status} for {package}")
    parse = parser or default_parser()
    d = parse(res.text, package, url)
    if not d.get("title"):
        raise RuntimeError(f"Play page for {package} parsed without a title (layout change?)")
    return d
