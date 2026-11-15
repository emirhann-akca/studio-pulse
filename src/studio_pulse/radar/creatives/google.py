"""Google Ads Transparency Center: per-creative rows for the tracked advertisers, weekly.

Source: the public BigQuery dataset `bigquery-public-data.google_ads_transparency_center`
(Google's own published dataset; BigQuery's terms, no scraping). Checked on 15 Nov 2026 with
`bq show --schema` and free `tabledata.list` samples: `creative_stats` has creative_id,
creative_page_url, ad_format_type (TEXT / IMAGE / VIDEO), region_stats (region_code,
first_shown, last_shown, times_shown_lower_bound / upper_bound, times_shown_start_date /
end_date, surface_serving_stats) and audience fields. There is NO video id, YouTube id, image
or text of the ad: the creative page (adstransparency.google.com/advertiser/<AR>/creative/<CR>)
is the only way to see it, and that page is a script application fed by undocumented internal
endpoints, which this package does not call. So Google rows are metadata plus a link.

times_shown_* bounds exist only for some regions (the EEA and a few countries such as TR in
the samples); the upper bound of the top bucket is INT64 max, so only the lower bound is used.

Bytes (dry runs on 15 Nov 2026, free): advertiser_id 3.94 GB, creative_id 3.94,
ad_format_type 1.04, region_code 2.25, first_shown 6.27, last_shown 6.27,
times_shown_lower_bound 3.78 (creative_page_url alone is 16.7 GB: it is rebuilt from the ids
instead; surface_serving_stats is 16+ GB and not read). The query names those seven columns:
27.49 GB, filtered by advertiser id only (the table is neither partitioned nor clustered, so a
WHERE clause does not lower the bill; a daily run would cost seven times as much, so Google is
weekly only). Dry-run first, refused above `radar.creatives.max_gb` (default 30, at most 50),
then run with `maximum_bytes_billed` set.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import duckdb

from ..adintel.google_atc import TABLE

_ID = re.compile(r"^AR\d{10,30}$")
_CR = re.compile(r"^CR\d{10,30}$")
PLACEHOLDER_ID = "AR00000000000000000000"  # dry run without resolved ids: same columns, same bytes


def page_url(advertiser_id: str, creative_id: str) -> str | None:
    if _ID.match(advertiser_id or "") and _CR.match(creative_id or ""):
        return (
            f"https://adstransparency.google.com/advertiser/{advertiser_id}/creative/{creative_id}"
        )
    return None


def build_creatives_sql(
    ids: list[str], today: dt.date, *, lookback_days: int, active_days: int, per_advertiser: int
) -> str:
    for i in ids:
        if not _ID.match(i):
            raise ValueError(f"unsafe advertiser id {i!r}")
    if not ids:
        raise ValueError("no advertiser ids")
    cutoff = (today - dt.timedelta(days=int(lookback_days))).isoformat()
    active = (today - dt.timedelta(days=int(active_days))).isoformat()
    in_list = ", ".join(f"'{i}'" for i in sorted(set(ids)))
    n = int(per_advertiser)
    return f"""
WITH c AS (
  SELECT advertiser_id, creative_id, ANY_VALUE(ad_format_type) AS fmt,
         MIN(r.first_shown) AS first_shown, MAX(r.last_shown) AS last_shown,
         COUNT(DISTINCT r.region_code) AS regions_n,
         ARRAY_AGG(DISTINCT r.region_code IGNORE NULLS ORDER BY r.region_code LIMIT 40) AS regions,
         MAX(r.times_shown_lower_bound) AS times_min,
         COUNTIF(r.times_shown_lower_bound IS NOT NULL) AS times_regions
  FROM {TABLE}, UNNEST(region_stats) AS r
  WHERE advertiser_id IN ({in_list})
  GROUP BY advertiser_id, creative_id
  HAVING MAX(r.last_shown) >= '{cutoff}'
)
SELECT * FROM c
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY advertiser_id
  ORDER BY (fmt = 'VIDEO') DESC, (last_shown >= '{active}') DESC,
           DATE_DIFF(SAFE.PARSE_DATE('%Y-%m-%d', last_shown),
                     SAFE.PARSE_DATE('%Y-%m-%d', first_shown), DAY) DESC,
           creative_id
) <= {n}
""".strip()


def _date(s: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def to_rows(
    result: list[dict[str, Any]], advertisers: list[dict[str, Any]], today: dt.date, now: Any
) -> list[dict[str, Any]]:
    """BigQuery rows -> mr_creative rows (advertiser key, name and genre from the tracked list)."""
    by_id: dict[str, dict[str, Any]] = {}
    for a in advertisers:
        for i, gid in enumerate(a.get("google_ids") or []):
            names = a.get("google_names") or []
            by_id[gid] = {**a, "disclosed": names[i] if i < len(names) else a["name"]}
    out = []
    for r in result:
        aid, cid = str(r.get("advertiser_id") or ""), str(r.get("creative_id") or "")
        if not _CR.match(cid) or aid not in by_id:
            continue
        a = by_id[aid]
        fmt = str(r.get("fmt") or "UNKNOWN").upper()
        out.append(
            {
                "creative_key": f"g-{cid}",
                "source": "google_atc",
                "creative_id": cid,
                "advertiser_key": a["advertiser_key"],
                "advertiser_id": aid,
                "advertiser_name": a["name"],
                "genre": a["genre"],
                "format": fmt if fmt in ("VIDEO", "IMAGE", "TEXT") else "UNKNOWN",
                "regions": [str(x)[:8] for x in (r.get("regions") or [])][:40],
                "regions_n": int(r.get("regions_n") or 0),
                "first_shown": _date(r.get("first_shown")),
                "last_shown": _date(r.get("last_shown")),
                "times_shown_min": (
                    int(r["times_min"]) if r.get("times_min") is not None else None
                ),
                "times_shown_regions": int(r.get("times_regions") or 0),
                "note": str(a.get("disclosed") or "")[:120] or None,  # the name Google shows
                "extract_date": today,
                "updated_at": now,
            }
        )
    return out


def replace_rows(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> int:
    """Replace the Google rows; a YouTube id the owner attached to a creative is kept."""
    from .. import store

    attached = dict(
        con.execute(
            "SELECT creative_key, youtube_id FROM mr_creative WHERE source = 'google_atc' "
            "AND youtube_id IS NOT NULL"
        ).fetchall()
    )
    rows = [{**r, "youtube_id": attached.get(r["creative_key"])} for r in rows]
    return store.replace(con, "mr_creative", "source = 'google_atc'", [], rows)
