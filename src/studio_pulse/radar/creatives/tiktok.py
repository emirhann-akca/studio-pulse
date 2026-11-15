"""TikTok Commercial Content API: a DISABLED connector, kept ready for an approved app.

Source check (15 Nov 2026):
- Commercial Content API (developers.tiktok.com/products/commercial-content-api): "the public
  and researchers" can query ads in the Commercial Content Library by advertiser name or
  keyword, only with an approved application (TikTok for Developers account, an online form,
  a reply in about two working days, then a client key). Data: EU countries only; first /
  last shown date, status, reach ("unique users seen" bucket), advertiser name; available
  until a year after the ad was last shown. Endpoint (doc "commercial-content-api-query-ads"):
  `POST https://open.tiktokapis.com/v2/research/adlib/ad/query/?fields=...`, bearer token from
  `POST /v2/oauth/token/` (client credentials), `max_count` at most 10, paging by `search_id`.
- TikTok's December 2025 developer-terms update defines this data as "CCL Data" tied to
  DSA Article 39; the owner must read those terms on approval and confirm the studio's use.
- library.tiktok.com/robots.txt disallows /ads, /api and everything else; TikTok's terms
  forbid automated collection without written permission; Creative Center (ads.tiktok.com)
  data comes from undocumented internal endpoints. So there is no scraping path, and nothing
  here runs without both `radar.creatives.tiktok.enabled: true` and the app's keys in
  $PULSE_TIKTOK_CLIENT_KEY / $PULSE_TIKTOK_CLIENT_SECRET.

Application path: developers.tiktok.com -> Products -> Commercial Content API -> Apply (an
official e-mail address; describe the studio's use). On approval, put the keys in the
environment, read the terms, set `radar.creatives.tiktok.enabled: true`.

Not stored: video or image URLs (`ad.videos` is not requested). The link kept is the public
library page of the ad id. Tested only on synthetic fixtures; never run live.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Callable
from typing import Any

import requests

from ...llm import redact

KEY_ENV = "PULSE_TIKTOK_CLIENT_KEY"
SECRET_ENV = "PULSE_TIKTOK_CLIENT_SECRET"
HOST = "https://open.tiktokapis.com"
TOKEN_URL = f"{HOST}/v2/oauth/token/"
QUERY_URL = f"{HOST}/v2/research/adlib/ad/query/"
FIELDS = (
    "ad.id,ad.first_shown_date,ad.last_shown_date,ad.status,ad.reach,"
    "advertiser.business_id,advertiser.business_name"
)
_AD = re.compile(r"^\d{5,25}$")


def library_url(ad_id: str) -> str | None:
    return (
        f"https://library.tiktok.com/ads/detail/?ad_id={ad_id}" if _AD.match(ad_id or "") else None
    )


def reach_floor(bucket: str | None) -> int | None:
    """'11K' -> 11000, '1.2M' -> 1200000, '100K+' -> 100000; anything else -> None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KkMm]?)\+?\s*", str(bucket or ""))
    if not m:
        return None
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def _day(s: Any) -> dt.date | None:
    t = str(s or "")
    try:
        return dt.date(int(t[:4]), int(t[4:6]), int(t[6:8])) if re.fullmatch(r"\d{8}", t) else None
    except ValueError:
        return None


class TikTokCommercialContent:
    def __init__(
        self,
        *,
        enabled: bool,
        countries: tuple[str, ...] = ("DE", "FR"),
        session: requests.Session | None = None,
        key: str | None = None,
        secret: str | None = None,
        max_pages: int = 3,
        timeout_s: float = 30.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.enabled = enabled
        self.countries = countries
        self.session = session or requests.Session()
        self.key = key if key is not None else os.environ.get(KEY_ENV) or None
        self.secret = secret if secret is not None else os.environ.get(SECRET_ENV) or None
        self.max_pages = max_pages
        self.timeout_s = timeout_s
        self.log = log
        self._token: str | None = None

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "message": "radar.creatives.tiktok.enabled is false "
                    "(needs an approved Commercial Content API application)"}  # fmt: skip
        if not (self.key and self.secret):
            # literal names: the message is printed as JSON and carries nothing read from a credential
            return {"status": "not_configured",
                    "message": "set PULSE_TIKTOK_CLIENT_KEY and PULSE_TIKTOK_CLIENT_SECRET"}  # fmt: skip
        return {"status": "ready"}

    def _secrets(self) -> tuple[str, ...]:
        return tuple(x for x in (self.key, self.secret, self._token) if x)

    def token(self) -> str:
        if self._token:
            return self._token
        r = self.session.post(
            TOKEN_URL,
            data={"client_key": self.key, "client_secret": self.secret,
                  "grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout_s,
        )  # fmt: skip
        if r.status_code != 200:
            raise RuntimeError(f"token HTTP {r.status_code}")
        tok = (r.json() or {}).get("access_token")
        if not tok:
            raise RuntimeError("token response without access_token")
        self._token = str(tok)
        return self._token

    def query(self, term: str, since: dt.date, today: dt.date) -> list[dict[str, Any]]:
        ads: list[dict[str, Any]] = []
        search_id = None
        for _ in range(self.max_pages):
            body: dict[str, Any] = {
                "filters": {
                    "ad_published_date_range": {
                        "min": max(since, today - dt.timedelta(days=364)).strftime("%Y%m%d"),
                        "max": today.strftime("%Y%m%d"),
                    },
                    "country_code": list(self.countries)[0] if self.countries else "DE",
                },
                "search_term": term[:50],
                "max_count": 10,
            }
            if search_id:
                body["search_id"] = search_id
            r = self.session.post(
                QUERY_URL,
                params={"fields": FIELDS},
                json=body,
                headers={"Authorization": f"Bearer {self.token()}"},
                timeout=self.timeout_s,
            )
            if r.status_code != 200:
                raise RuntimeError(f"query HTTP {r.status_code}: {r.text[:200]}")
            data = (r.json() or {}).get("data") or {}
            ads += [a for a in data.get("ads") or [] if isinstance(a, dict)]
            search_id = data.get("search_id")
            if str(data.get("has_more")).lower() != "true" or not search_id:
                break
        return ads

    def refresh(
        self,
        advertisers: list[dict[str, Any]],
        terms: dict[str, tuple[str, ...]],
        since: dt.date,
        today: dt.date,
        now: Any,
    ) -> dict[str, Any]:
        st = self.status()
        if st["status"] != "ready":
            return {**st, "rows": []}
        rows: dict[str, dict[str, Any]] = {}
        try:
            for adv in advertisers:
                for term in terms.get(adv["advertiser_key"], ()):
                    for a in self.query(term, since, today):
                        rows.update(to_rows([a], adv, self.countries, today, now))
        except Exception as exc:
            msg = redact(f"{type(exc).__name__}: {exc}", self._secrets())[:400]
            return {"status": "error", "message": msg, "rows": list(rows.values())}
        return {"status": "ok", "rows": list(rows.values()), "message": ""}


def to_rows(
    ads: list[dict[str, Any]], adv: dict[str, Any], countries: tuple[str, ...], today: dt.date,
    now: Any,
) -> dict[str, dict[str, Any]]:  # fmt: skip
    out = {}
    for item in ads:
        ad = item.get("ad") or {}
        ad_id = str(ad.get("id") or "")
        if not _AD.match(ad_id):
            continue
        bucket = str((ad.get("reach") or {}).get("unique_users_seen") or "")[:12] or None
        out[f"t-{ad_id}"] = {
            "creative_key": f"t-{ad_id}",
            "source": "tiktok",
            "creative_id": ad_id,
            "advertiser_key": adv["advertiser_key"],
            "advertiser_id": str((item.get("advertiser") or {}).get("business_id") or "")[:25]
            or None,
            "advertiser_name": adv["name"],
            "genre": adv["genre"],
            "format": "VIDEO",
            "regions": list(countries)[:1],
            "regions_n": 1,
            "first_shown": _day(ad.get("first_shown_date")),
            "last_shown": _day(ad.get("last_shown_date")),
            "eu_reach": reach_floor(bucket),
            "reach_bucket": bucket,
            "platforms": ["tiktok"],
            "extract_date": today,
            "updated_at": now,
        }
    return out
