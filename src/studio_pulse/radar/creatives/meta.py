"""Meta Ad Library API: per-ad rows for the creative library (lights up with a token).

Source check (15 Nov 2026): `GET graph.facebook.com/{version}/ads_archive` is Meta's
documented Ad Library API (developers.facebook.com/docs/graph-api/reference/ads_archive and
/archived-ad). It needs an access token from a Meta developer app with the Ad Library API
enabled (identity confirmation) in $PULSE_META_AD_LIBRARY_TOKEN. www.facebook.com/robots.txt
forbids automated collection without written permission, so nothing on facebook.com is ever
fetched: this is an authenticated client of the API only. Outside political ads the API
returns ads delivered in the EU (the Digital Services Act), so `ad_reached_countries` names EU
countries, and `eu_total_reach` (EU-only, an estimate per ad over its whole run) is kept.

NOT RUN LIVE (the studio has no token yet): written against the field reference and tested on
recorded-shape synthetic fixtures (tests/fixtures/radar/creatives/meta_*.json).

Not stored: `ad_snapshot_url` (it is not requested: Meta's render URL carries the access
token); the link kept is the public Ad Library page of the ad id. Ad text (bodies, titles) is
kept, cut to 500 / 150 characters, on this machine only, and always shown escaped.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from ..adintel.google_atc import fold
from ..adintel.meta_library import MetaAdLibrary

FIELDS = (
    "id",
    "page_id",
    "page_name",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_creative_link_captions",
    "publisher_platforms",
    "languages",
    "eu_total_reach",
    "age_country_gender_reach_breakdown",
)
_AD = re.compile(r"^\d{5,25}$")
_CC = re.compile(r"^[A-Z]{2}$")


def library_url(ad_id: str) -> str | None:
    return f"https://www.facebook.com/ads/library/?id={ad_id}" if _AD.match(ad_id or "") else None


def _day(s: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _countries(ad: dict[str, Any]) -> list[str]:
    out: set[str] = set()
    for b in ad.get("age_country_gender_reach_breakdown") or []:
        c = str((b or {}).get("country") or "").upper() if isinstance(b, dict) else ""
        if _CC.match(c):
            out.add(c)
    return sorted(out)


def _first(xs: Any, n: int) -> str | None:
    for x in xs or []:
        t = " ".join(str(x).split())
        if t:
            return t[:n]
    return None


def to_rows(
    ads: list[dict[str, Any]],
    adv: dict[str, Any],
    today: dt.date,
    now: Any,
    page_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """API ads -> mr_creative rows. `page_filter`: folded terms; with a keyword search an ad
    is kept only when its page name is, or starts with, one of them (the game's own page:
    "Pebble Sort (synthetic)"; a fan page mentioning the game is not the advertiser's ad)."""
    rows = []
    for a in ads:
        ad_id = str(a.get("id") or "")
        if not _AD.match(ad_id):
            continue
        page = fold(str(a.get("page_name") or ""))
        if page_filter and not any(page == t or page.startswith(t + " ") for t in page_filter):
            continue
        start = _day(a.get("ad_delivery_start_time"))
        stop = _day(a.get("ad_delivery_stop_time"))
        reach = a.get("eu_total_reach")
        regions = _countries(a)
        plats = sorted(
            {str(p).lower()[:24] for p in a.get("publisher_platforms") or [] if str(p).strip()}
        )
        langs = sorted(
            {str(x).lower()[:5] for x in a.get("languages") or [] if re.fullmatch(r"[A-Za-z]{2,3}", str(x))}
        )  # fmt: skip
        rows.append(
            {
                "creative_key": f"m-{ad_id}",
                "source": "meta",
                "creative_id": ad_id,
                "advertiser_key": adv["advertiser_key"],
                "advertiser_id": str(a.get("page_id") or "")[:25] or None,
                "advertiser_name": adv["name"],
                "genre": adv["genre"],
                "format": "VIDEO",  # queried with media_type=VIDEO
                "regions": regions,
                "regions_n": len(regions),
                "first_shown": start,
                # no stop time: still delivering (the Library's "active"), seen up to today
                "last_shown": stop or today,
                "eu_reach": int(reach) if isinstance(reach, int) and reach >= 0 else None,
                "platforms": plats,
                "languages": langs,
                "ad_text": _first(a.get("ad_creative_bodies"), 500),
                "ad_title": _first(a.get("ad_creative_link_titles"), 150)
                or _first(a.get("ad_creative_link_captions"), 150),
                "note": str(a.get("page_name") or "")[:120] or None,
                "extract_date": today,
                "updated_at": now,
            }
        )
    return rows


def refresh(
    advertisers: list[dict[str, Any]],
    terms: dict[str, tuple[str, ...]],
    *,
    countries: tuple[str, ...],
    version: str,
    max_pages: int,
    since: dt.date,
    today: dt.date,
    now: Any,
    session: Any = None,
    token: str | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """One pass over the tracked advertisers with page ids or search terms."""
    lib = MetaAdLibrary(
        version=version,
        countries=list(countries),
        token=token,
        session=session,
        max_pages=max_pages,
        log=log,
        today=today,
    )
    if not lib.token:
        return {"status": "not_configured", "rows": [], "message": lib.fetch()["message"]}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    queries = 0
    for adv in advertisers:
        jobs: list[tuple[dict[str, Any], list[str] | None]] = []
        pages = [p for p in adv.get("meta_page_ids") or [] if _AD.match(str(p))][:10]
        if pages:
            jobs.append(({"page_ids": pages}, None))
        for t in terms.get(adv["advertiser_key"], ()):
            jobs.append(({"search_terms": t}, [fold(t)]))
        for kw, page_filter in jobs:
            queries += 1
            res = lib.fetch(
                **kw, fields=FIELDS, delivery_date_min=since.isoformat(), media_type="VIDEO"
            )
            if res["status"] != "ok":
                errors.append(f"{adv['advertiser_key']}: {res.get('message')}")
                continue
            rows += to_rows(res["ads"], adv, today, now, page_filter)
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        seen[r["creative_key"]] = r
    status = "ok" if not errors else ("error" if not seen else "partial")
    return {
        "status": status,
        "rows": list(seen.values()),
        "queries": queries,
        "message": "; ".join(errors)[:400],
    }
