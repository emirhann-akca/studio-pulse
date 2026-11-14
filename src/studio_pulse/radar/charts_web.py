"""Apple top charts from the App Store web pages (apps.apple.com), the radar's default chart
source.

`apps.apple.com/robots.txt` allows the chart pages (it lists chart sitemaps) and disallows
`/WebObjects/*`, `/api/*`, `/includes/*`, `/v1/*` and search; the legacy iTunes RSS lists
(`itunes.apple.com/{cc}/rss/...`) are disallowed by `itunes.apple.com/robots.txt`
(`/*/rss/*`), so the RSS source is only used to replay recorded fixtures.

One page per storefront and genre, `https://apps.apple.com/{cc}/iphone/charts/{genre}`
(found 14 Nov 2026), is server-rendered with a `serialized-server-data` JSON block that holds
both segments, "Top Free Games" (`chart=top-free`) and "Top Paid Games" (`top-paid`): the first
25 apps in full (id, name, bundle id) and the ids of ranks 26-200 in order
(`nextPage.remainingContent`). The page's own continuation calls `/v1/catalog/...`, which
robots.txt disallows, so the radar never follows it: one request gives both lists to rank 200.
There is no top-grossing chart on the web and no new-games chart; configured lists of those
kinds are skipped with a note. Names, bundle ids and developers of ranks 26+ come from the
radar's own Lookup of charted apps (`fill_from_lookup`).

Rows go to raw_mr_chart with the legacy chart names (`topfreeapplications`,
`toppaidapplications`) so scoring, trends and the digest are unchanged; `source_url` is the page.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import duckdb

from . import store
from .http import BudgetExhausted, Fetcher, HostStopped, RobotsDisallowed

HOST = "apps.apple.com"
PAGE_URL = "https://apps.apple.com/{cc}/iphone/charts/{genre}"
SEGMENT_TO_CHART = {"top-free": "topfreeapplications", "top-paid": "toppaidapplications"}
CHART_TO_SEGMENT = {v: k for k, v in SEGMENT_TO_CHART.items()}
_DATA = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.S | re.IGNORECASE
)


class ChartPageError(RuntimeError):
    pass


def page_url(cc: str, genre: str) -> str:
    return PAGE_URL.format(cc=cc.lower(), genre=str(genre))


def parse_page(html: str) -> dict[str, list[dict[str, Any]]]:
    """{'top-free': [{rank, app_id, name, bundle_id}], 'top-paid': [...]} from one chart page."""
    m = _DATA.search(html)
    if not m:
        raise ChartPageError("no serialized-server-data block (page layout changed?)")
    doc = json.loads(m.group(1))
    try:
        segments = doc["data"][0]["data"]["segments"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChartPageError("chart page without segments (page layout changed?)") from exc
    out: dict[str, list[dict[str, Any]]] = {}
    for seg in segments:
        chart = seg.get("chart")
        if chart not in SEGMENT_TO_CHART:
            continue
        # every position counts toward the rank (Apple's own ordinal); an app bundle in the
        # list keeps its position but is not stored (it is not an app the radar can look up)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        pos = 0
        for shelf in seg.get("shelves") or []:
            for it in shelf.get("items") or []:
                pos += 1
                aid = str(it.get("adamId") or "")
                if aid and aid not in seen and it.get("$kind", "Lockup") == "Lockup":
                    seen.add(aid)
                    rows.append(
                        {
                            "app_id": aid,
                            "name": it.get("title"),
                            "bundle_id": it.get("bundleId"),
                            "rank": pos,
                        }
                    )
        for r in (seg.get("nextPage") or {}).get("remainingContent") or []:
            pos += 1
            aid = str(r.get("id") or "")
            if aid and aid not in seen and r.get("type", "apps") == "apps":
                seen.add(aid)
                rows.append({"app_id": aid, "name": None, "bundle_id": None, "rank": pos})
        out[chart] = rows
    if not out:
        raise ChartPageError("chart page without top-free or top-paid segments")
    return out


def stage(
    con: duckdb.DuckDBPyConnection,
    f: Fetcher,
    s: Any,
    day: dt.date,
    res: Any,
    log: Callable[[str], None],
) -> None:
    """Fetch every configured (country, genre) page once and store the configured lists."""
    n = 0
    skipped = sorted(
        {c.key for c in s.charts if c.chart not in CHART_TO_SEGMENT},
    )
    if skipped:
        res.notes.append(
            "apps.apple.com has no "
            + ", ".join(skipped)
            + " chart (no top-grossing or new-games list on the web): skipped"
        )
    for cc in s.countries:
        by_genre: dict[str, list[Any]] = {}
        for spec in s.charts_for(cc):
            if spec.chart in CHART_TO_SEGMENT:
                by_genre.setdefault(spec.genre, []).append(spec)
        for genre, specs in by_genre.items():
            url = page_url(cc, genre)
            try:
                page = f.get(url, f"web_chart:{cc}:{genre}")
                if page.status != 200:
                    raise ChartPageError(f"{url} answered {page.status}")
                lists = parse_page(page.text)
            except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
                res.notes.append(f"web chart {cc} {genre}: {exc}")
                log(f"    radar: web chart {cc} {genre}: {exc}")
                res.counts["chart_rows"] = n
                return
            except Exception as exc:
                res.errors.append(f"web chart {cc} {genre}: {exc}")
                log(f"    radar: web chart {cc} {genre}: {exc}")
                continue
            fetched = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
            for spec in specs:
                ranked = [r for r in lists.get(CHART_TO_SEGMENT[spec.chart], [])]
                kept = [r for r in ranked if r["rank"] <= spec.limit]
                sha = hashlib.sha256(
                    json.dumps([r["app_id"] for r in ranked]).encode("utf-8")
                ).hexdigest()
                rows = [
                    {
                        "snapshot_date": day,
                        "fetched_at": fetched,
                        "store": "apple",
                        "country": cc.lower(),
                        "chart": spec.chart,
                        "genre_id": str(genre),
                        "requested_limit": int(spec.limit),
                        "n_rows": len(kept),
                        "rank": r["rank"],
                        "app_id": r["app_id"],
                        "bundle_id": r["bundle_id"],
                        "name": r["name"],
                        "developer": None,
                        "developer_id": None,
                        "release_date_feed": None,
                        "category_id": None,
                        # a web page has no feed timestamp: it is rendered at request time
                        "feed_updated": fetched,
                        "source_url": url,
                        "payload_sha256": sha,
                    }
                    for r in kept
                ]
                store.replace(
                    con,
                    "raw_mr_chart",
                    "snapshot_date = ? AND store = 'apple' AND country = ? AND chart = ? "
                    "AND genre_id = ?",
                    [day, cc.lower(), spec.chart, str(genre)],
                    rows,
                )
                n += len(rows)
    res.counts["chart_rows"] = n
    log(f"    radar: {n} chart rows (apps.apple.com)")


def fill_from_lookup(con: duckdb.DuckDBPyConnection, day: dt.date) -> int:
    """Names, bundle ids and developers of web chart rows from the day's
    Lookup rows of the same storefront (the web page names only its first 25 apps)."""
    before = con.execute(
        "SELECT COUNT(*) FROM raw_mr_chart WHERE snapshot_date = ? AND name IS NULL", [day]
    ).fetchone()[0]
    con.execute(
        """
        UPDATE raw_mr_chart AS c SET
          name = COALESCE(c.name, a.name),
          bundle_id = COALESCE(c.bundle_id, a.bundle_id),
          developer = COALESCE(c.developer, a.developer),
          developer_id = COALESCE(c.developer_id, a.developer_id)
        FROM (
          SELECT country, app_id, any_value(name) AS name, any_value(bundle_id) AS bundle_id,
                 any_value(developer) AS developer, any_value(developer_id) AS developer_id
          FROM raw_mr_app WHERE snapshot_date = ? AND store = 'apple' GROUP BY 1, 2
        ) AS a
        WHERE c.snapshot_date = ? AND c.country = a.country AND c.app_id = a.app_id
          AND c.source_url LIKE 'https://apps.apple.com/%'
        """,
        [day, day],
    )
    after = con.execute(
        "SELECT COUNT(*) FROM raw_mr_chart WHERE snapshot_date = ? AND name IS NULL", [day]
    ).fetchone()[0]
    return int(before - after)


def is_web_source(url: str | None) -> bool:
    return bool(url) and str(url).startswith("https://apps.apple.com/")


__all__ = [
    "ChartPageError",
    "fill_from_lookup",
    "page_url",
    "parse_page",
    "stage",
]
