"""Meta Ad Library API (`GET graph.facebook.com/{version}/ads_archive`) for a game dossier.

NOT RUN LIVE: written against the public API reference and tested only on recorded-shape
fixtures with synthetic values (tests/fixtures/radar/dossier/meta/). The studio has no token
yet. Outside political ads, the Ad Library API returns ads delivered in the EU (the Digital
Services Act transparency rules), so `ad_reached_countries` must name EU countries.

Access: an Ad Library API token (a Meta developer account with identity confirmation) in
$PULSE_META_AD_LIBRARY_TOKEN; it is sent as a query parameter (the API's only way), never
logged, never written to the ledger, cache or dossier, and redacted from errors.
graph.facebook.com/robots.txt disallows all crawling; this is an authenticated API client
under Meta's Platform Terms, not a crawler, and it reads only what the API returns.

Fields requested (check them against the reference when the token arrives): id, page_id,
page_name, ad_creation_time, ad_delivery_start_time, ad_delivery_stop_time,
ad_creative_bodies, ad_creative_link_titles, ad_snapshot_url, publisher_platforms, languages,
eu_total_reach. Paging follows `paging.next` up to `max_pages`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import requests

from ...llm import redact

TOKEN_ENV = "PULSE_META_AD_LIBRARY_TOKEN"
GRAPH = "https://graph.facebook.com"
FIELDS = (
    "id",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_snapshot_url",
    "publisher_platforms",
    "languages",
    "eu_total_reach",
)


def archive_url(
    version: str,
    *,
    countries: list[str],
    search_terms: str | None = None,
    page_ids: list[str] | None = None,
    limit: int = 100,
) -> str:
    """The first page's URL without the token (the token is added at send time)."""
    q: dict[str, Any] = {
        "ad_type": "ALL",
        "ad_active_status": "ALL",
        "ad_reached_countries": json.dumps([c.upper() for c in countries]),
        "fields": ",".join(FIELDS),
        "limit": int(limit),
    }
    if page_ids:
        q["search_page_ids"] = json.dumps([str(p) for p in page_ids])
    elif search_terms:
        q["search_terms"] = search_terms
        q["search_type"] = "KEYWORD_EXACT_PHRASE"
    else:
        raise ValueError("search_terms or page_ids is required")
    return f"{GRAPH}/{version}/ads_archive?{urlencode(q)}"


def _day(s: Any) -> str | None:
    return str(s)[:10] if s else None


def summarize(ads: list[dict[str, Any]], today: dt.date) -> dict[str, Any]:
    pages: dict[str, dict[str, Any]] = {}
    for a in ads:
        p = pages.setdefault(
            str(a.get("page_id")),
            {
                "page_id": str(a.get("page_id")),
                "page_name": a.get("page_name"),
                "ads": 0,
                "active": 0,
                "platforms": {},
                "first_start": None,
                "last_start": None,
                "eu_reach": 0,
                "examples": [],
            },
        )
        p["ads"] += 1
        stop = _day(a.get("ad_delivery_stop_time"))
        if stop is None or stop >= today.isoformat():
            p["active"] += 1
        for pl in a.get("publisher_platforms") or []:
            p["platforms"][pl] = p["platforms"].get(pl, 0) + 1
        st = _day(a.get("ad_delivery_start_time"))
        if st:
            p["first_start"] = st if p["first_start"] is None else min(p["first_start"], st)
            p["last_start"] = st if p["last_start"] is None else max(p["last_start"], st)
        if isinstance(a.get("eu_total_reach"), int):
            p["eu_reach"] += a["eu_total_reach"]
        if len(p["examples"]) < 3:
            body = (a.get("ad_creative_bodies") or [""])[0] or ""
            p["examples"].append(
                {
                    "start": st,
                    "text": " ".join(body.split()[:25]),
                    "snapshot": a.get("ad_snapshot_url"),
                }
            )
    return {
        "pages": sorted(pages.values(), key=lambda p: (-p["ads"], p["page_id"])),
        "ads": len(ads),
        "basis": "platform_reported",
    }


class MetaAdLibrary:
    def __init__(
        self,
        *,
        version: str = "v24.0",
        countries: list[str] | None = None,
        token: str | None = None,
        session: requests.Session | None = None,
        max_pages: int = 3,
        timeout_s: float = 30.0,
        log: Callable[[str], None] = print,
        today: dt.date | None = None,
    ) -> None:
        self.version = version
        self.countries = countries or ["DE", "FR"]
        self.token = token if token is not None else os.environ.get(TOKEN_ENV) or None
        self.session = session or requests.Session()
        self.max_pages = max_pages
        self.timeout_s = timeout_s
        self.log = log
        self.today = today or dt.date.today()

    def search(
        self, *, search_terms: str | None = None, page_ids: list[str] | None = None
    ) -> dict[str, Any]:
        base = {
            "basis": "platform_reported",
            "source": "meta_ad_library",
            "verified_live": False,  # never run against the live API yet
            "countries": self.countries,
            "query": search_terms or page_ids,
        }
        if not self.token:
            return {
                **base,
                "status": "not_configured",
                "message": f"no token: set {TOKEN_ENV} (Meta Ad Library API access)",
            }
        url: str | None = archive_url(
            self.version, countries=self.countries, search_terms=search_terms, page_ids=page_ids
        )
        ads: list[dict[str, Any]] = []
        pages = 0
        try:
            while url and pages < self.max_pages:
                sep = "&" if "?" in url else "?"
                full = url if "access_token=" in url else f"{url}{sep}access_token={self.token}"
                r = self.session.get(full, timeout=self.timeout_s)
                pages += 1
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                doc = r.json()
                ads += list(doc.get("data") or [])
                url = (doc.get("paging") or {}).get("next")
        except Exception as exc:
            return {
                **base,
                "status": "error",
                "message": redact(f"{type(exc).__name__}: {exc}", (self.token,))[:400],
            }
        return {**base, "status": "ok", "pages_read": pages, **summarize(ads, self.today)}
