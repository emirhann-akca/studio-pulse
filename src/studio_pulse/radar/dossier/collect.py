"""Store data for one game: Apple Lookup per storefront, the Google Play details page (fields
and its embedded reviews), screenshots into a local cache, and what the radar's warehouse
already knows (chart positions, rating reads, versions seen, tags, mechanic shares).

Every request goes through the radar's polite fetcher (robots.txt for Play and iTunes,
spacing, daily budgets, the request ledger). The raw Play page is parsed in memory and never
stored; screenshots are written only to the local cache under the state directory.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .. import apple, play
from ..http import BudgetExhausted, Fetcher, HostStopped, RobotsDisallowed
from . import reviews as reviews_mod

_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


class TargetError(ValueError):
    pass


@dataclass
class Target:
    raw: str
    apple_id: str | None = None
    play_package: str | None = None

    @property
    def key(self) -> str:
        """A URL- and file-safe key: apple-<id>, else play-<package>."""
        if self.apple_id:
            return f"apple-{self.apple_id}"
        return f"play-{self.play_package}"

    @property
    def app_key(self) -> str | None:
        return f"apple:{self.apple_id}" if self.apple_id else None


def parse_target(text: str) -> Target:
    t = text.strip()
    m = re.fullmatch(r"(?:id)?(\d{5,})", t)
    if m:
        return Target(raw=t, apple_id=m.group(1))
    m = re.search(r"apps\.apple\.com/.*/id(\d{5,})", t)
    if m:
        return Target(raw=t, apple_id=m.group(1))
    m = re.search(r"[?&]id=([A-Za-z0-9_.]+)", t)
    if m and "play.google.com" in t:
        t = m.group(1)
    if _PACKAGE.match(t):
        return Target(raw=text, play_package=t)
    raise TargetError(f"{text!r} is neither an Apple app id nor a Play package name")


def _rows(con: duckdb.DuckDBPyConnection | None, sql: str, params: list[Any]) -> list[dict]:
    if con is None:
        return []
    try:
        cur = con.execute(sql, params)
    except duckdb.CatalogException:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def resolve(con: duckdb.DuckDBPyConnection | None, f: Fetcher, target: Target, cc: str) -> Target:
    """Fill in the other store's id: the radar's iOS->Play map, else the shared bundle id."""
    if target.apple_id and not target.play_package:
        hit = _rows(
            con,
            "SELECT play_id FROM raw_mr_xref WHERE apple_id = ? AND status = 'mapped' "
            "ORDER BY verified_on DESC LIMIT 1",
            [target.apple_id],
        )
        if hit:
            target.play_package = hit[0]["play_id"]
    elif target.play_package and not target.apple_id:
        hit = _rows(
            con,
            "SELECT apple_id FROM raw_mr_xref WHERE play_id = ? AND status = 'mapped' "
            "ORDER BY verified_on DESC LIMIT 1",
            [target.play_package],
        )
        if hit:
            target.apple_id = hit[0]["apple_id"]
        else:
            found = apple.lookup_bundle(f, target.play_package, cc)
            if found:
                target.apple_id = str(found["trackId"])
    return target


# -- Apple -----------------------------------------------------------------------------------


def apple_data(f: Fetcher, apple_id: str, countries: list[str]) -> dict[str, Any]:
    """One Lookup per storefront (served from the radar's day cache when it already asked)."""
    per: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    missing: list[str] = []
    for cc in countries:
        found, _ = apple.lookup_ids(f, [apple_id], cc, purpose=f"dossier_lookup:{cc}")
        if not found:
            missing.append(cc)
            continue
        r = found[0]
        first = first or r
        per.append(
            {
                "country": cc,
                "rating_count": r.get("userRatingCount"),
                "rating_avg": r.get("averageUserRating"),
                "rating_count_cur_ver": r.get("userRatingCountForCurrentVersion"),
                "version": r.get("version"),
                "current_version_date": str(r.get("currentVersionReleaseDate") or "")[:10] or None,
                "release_date": str(r.get("releaseDate") or "")[:10] or None,
                "price": r.get("price"),
                "currency": r.get("currency"),
                "basis": "observed",
                "source": "itunes_lookup",
            }
        )
    if first is None:
        return {"found": False, "missing": missing}
    return {
        "found": True,
        "missing": missing,
        "name": first.get("trackName"),
        "developer": first.get("artistName"),
        "seller": first.get("sellerName"),
        "developer_id": str(first.get("artistId") or "") or None,
        "bundle_id": first.get("bundleId"),
        "primary_genre": first.get("primaryGenreName"),
        "genres": list(first.get("genres") or []),
        "description": first.get("description") or "",
        "release_notes": first.get("releaseNotes") or "",
        "content_rating": first.get("contentAdvisoryRating"),
        "file_size_mb": round(int(first["fileSizeBytes"]) / 1e6, 1)
        if str(first.get("fileSizeBytes") or "").isdigit()
        else None,
        "store_url": (first.get("trackViewUrl") or "").split("?")[0] or None,
        "screenshot_urls": list(first.get("screenshotUrls") or [])
        or list(first.get("ipadScreenshotUrls") or []),
        "storefronts": per,
    }


# -- Google Play -------------------------------------------------------------------------------


def play_data(f: Fetcher, package: str, country: str, lang: str, parser=None) -> dict[str, Any]:
    """Details fields and the embedded reviews from one details page (robots-checked)."""
    url = play.details_url(package, country, lang)
    res = f.get(url, "dossier_play", cacheable=False)
    if res.status == 404:
        return {"found": False}
    if res.status != 200:
        raise RuntimeError(f"Play details answered {res.status} for {package}")
    parse = parser or play.default_parser()
    html = res.text
    d = parse(html, package, url)
    if not d.get("title"):
        raise RuntimeError(f"Play page for {package} parsed without a title (layout change?)")
    row = play.app_row(d, dt.date.today(), country)  # drops the developer's personal fields
    return {
        "found": True,
        "row": row,
        "screenshot_urls": list(d.get("screenshots") or []),
        "reviews": reviews_mod.parse_play_reviews(html),
        "updated": row["current_version_date"].isoformat() if row["current_version_date"] else None,
    }


# -- screenshots ---------------------------------------------------------------------------------


def _mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def screenshots(
    f: Fetcher, urls: list[str], cache: Path, n: int, log=print
) -> list[dict[str, Any]]:
    """Download up to n screenshots into the local cache (once: a cached file is reused)."""
    out: list[dict[str, Any]] = []
    cache.mkdir(parents=True, exist_ok=True)
    for i, u in enumerate(urls[:n]):
        sid = f"s{i + 1}"
        h = hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
        hit = sorted(cache.glob(f"{h}.*"))
        if hit:
            data = hit[0].read_bytes()
            out.append(
                {"id": sid, "url": u, "file": str(hit[0]), "mime": _mime(data), "bytes": len(data)}
            )
            continue
        try:
            res = f.get(u, "dossier_screenshot", cacheable=False)
        except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
            log(f"    dossier: screenshot {sid} not fetched ({exc})")
            break
        mime = _mime(res.content) if res.status == 200 else None
        if not mime:
            log(f"    dossier: screenshot {sid} answered {res.status} / not an image")
            continue
        p = cache / f"{h}.{mime.split('/')[1]}"
        p.write_bytes(res.content)
        out.append({"id": sid, "url": u, "file": str(p), "mime": mime, "bytes": len(res.content)})
    # ids follow what was actually kept, so s1..sN always match the images sent
    for i, s in enumerate(out):
        s["id"] = f"s{i + 1}"
    return out


# -- what the radar already knows -------------------------------------------------------------


def warehouse_context(
    con: duckdb.DuckDBPyConnection | None, target: Target, day: dt.date
) -> dict[str, Any]:
    aid, pkg = target.apple_id, target.play_package
    ctx: dict[str, Any] = {"available": False}
    days = _rows(
        con,
        "SELECT MAX(snapshot_date) AS d, COUNT(DISTINCT snapshot_date) AS n "
        "FROM stg_mr_chart WHERE snapshot_date <= ?",
        [day],
    )
    if not days or days[0]["d"] is None:
        return ctx
    last = days[0]["d"]
    ctx.update(
        {
            "available": True,
            "latest_day": _iso(last),
            "history_days": int(days[0]["n"]),
            "basis": "observed",
        }
    )
    if aid:
        ctx["ranks_today"] = [
            {**r, "basis": "observed", "source": "apple_rss"}
            for r in _rows(
                con,
                "SELECT country, chart || ':' || genre_id AS list, rank, list_len "
                "FROM stg_mr_chart WHERE snapshot_date = ? AND app_id = ? ORDER BY rank, country, list",
                [last, aid],
            )
        ]
        hist = _rows(
            con,
            "SELECT COUNT(DISTINCT snapshot_date) AS days, MIN(snapshot_date) AS first_seen, "
            "MIN(rank) AS best_rank FROM stg_mr_chart WHERE app_id = ? AND snapshot_date <= ?",
            [aid, day],
        )
        if hist and hist[0]["days"]:
            ctx["chart_history"] = {
                "days_charted": hist[0]["days"],
                "first_seen": _iso(hist[0]["first_seen"]),
                "best_rank": hist[0]["best_rank"],
                "basis": "observed",
            }
        b = _rows(
            con,
            "SELECT radar_class, momentum, age_days, climb_max, spread, rv_max, iv "
            "FROM fct_mr_breakout_daily WHERE app_id = ? AND snapshot_date = ?",
            [aid, last],
        )
        if b:
            r = b[0]
            ctx["breakout"] = {
                "class": r["radar_class"],
                "momentum": round(r["momentum"], 4) if r["momentum"] is not None else None,
                "age_days": r["age_days"],
                "spread": r["spread"],
                "rv_max": round(r["rv_max"], 1) if r["rv_max"] is not None else None,
                "iv": round(r["iv"], 1) if r["iv"] is not None else None,
                "basis": "rule_score",
            }
        ctx["rating_reads"] = [
            {**r, "snapshot_date": _iso(r["snapshot_date"]), "basis": "observed"}
            for r in _rows(
                con,
                "SELECT snapshot_date, country, MAX(rating_count) AS rating_count FROM raw_mr_app "
                "WHERE store = 'apple' AND app_id = ? AND snapshot_date <= ? GROUP BY 1, 2 "
                "ORDER BY 1, 2",
                [aid, day],
            )
        ]
        ctx["tags"] = _rows(
            con,
            "SELECT source, dimension, value, field, quote FROM fct_mr_app_tags "
            "WHERE app_key = ? ORDER BY dimension, value",
            [f"apple:{aid}"],
        )
    ids = [x for x in (aid, pkg) if x]
    if ids:
        ctx["versions_seen"] = [
            {
                "store": r["store"],
                "version": r["version"],
                "date": _iso(r["current_version_date"]),
                "first_snapshot": _iso(r["first_snapshot"]),
                "basis": "observed",
            }
            for r in _rows(
                con,
                "SELECT store, version, current_version_date, MIN(snapshot_date) AS first_snapshot "
                "FROM raw_mr_app WHERE app_id IN (SELECT UNNEST(?)) AND version IS NOT NULL "
                "AND snapshot_date <= ? GROUP BY 1, 2, 3 ORDER BY 3, 1",
                [ids, day],
            )
        ]
    if pkg:
        inst = _rows(
            con,
            "SELECT snapshot_date, installs_bucket, real_installs, iv FROM stg_mr_install_daily "
            "WHERE play_id = ? AND snapshot_date <= ? ORDER BY snapshot_date",
            [pkg, day],
        )
        ctx["installs"] = [
            {
                "snapshot_date": _iso(r["snapshot_date"]),
                "bucket": r["installs_bucket"],
                "real_installs": r["real_installs"],
                "per_day": round(r["iv"], 1) if r["iv"] is not None else None,
                "basis": "store_reported",
            }
            for r in inst
        ]
    return ctx


def market_context(
    con: duckdb.DuckDBPyConnection | None, mechanic: str | None, day: dt.date, exclude: str | None
) -> dict[str, Any]:
    """Chart share trend and saturation of one mechanic in the radar's lists."""
    out: dict[str, Any] = {"mechanic": mechanic, "available": False}
    if not mechanic:
        return out
    last = _rows(
        con, "SELECT MAX(snapshot_date) AS d FROM stg_mr_chart WHERE snapshot_date <= ?", [day]
    )
    if not last or last[0]["d"] is None:
        return out
    d = last[0]["d"]
    out["available"] = True
    out["basis"] = "observed"
    out["day"] = _iso(d)
    trend = _rows(
        con,
        "SELECT week, country, share, prior_mean, delta, z, n_apps, prior_weeks_seen, is_trend, "
        "tagged_share FROM fct_mr_trend_weekly WHERE mechanic = ? AND week = "
        "(SELECT MAX(week) FROM fct_mr_trend_weekly) ORDER BY country",
        [mechanic],
    )
    out["trend"] = [
        {
            "week": _iso(r["week"]),
            "country": r["country"],
            "share": round(r["share"], 4),
            "prior_mean": None if r["prior_mean"] is None else round(r["prior_mean"], 4),
            "delta": None if r["delta"] is None else round(r["delta"], 4),
            "z": None if r["z"] is None else round(r["z"], 2),
            "n_apps": r["n_apps"],
            "prior_weeks": r["prior_weeks_seen"],
            "is_trend": bool(r["is_trend"]),
            "tagged_share": None if r["tagged_share"] is None else round(r["tagged_share"], 3),
            "basis": "observed",
        }
        for r in trend
    ]
    sim = _rows(
        con,
        """
        WITH tagged AS (
          SELECT DISTINCT replace(app_key, 'apple:', '') AS app_id FROM fct_mr_app_tags
          WHERE dimension IN ('mechanic_primary', 'mechanic') AND value = ?
        ), rows AS (
          SELECT c.app_id, c.developer, c.chart, c.country, c.rank
          FROM stg_mr_chart AS c JOIN tagged USING (app_id) WHERE c.snapshot_date = ?
        )
        SELECT app_id, ANY_VALUE(developer) AS developer, COUNT(*) AS chart_rows,
               MIN(rank) AS best_rank,
               COUNT(*) FILTER (WHERE chart = 'topgrossingapplications') AS grossing_rows,
               (SELECT ANY_VALUE(name) FROM stg_mr_chart s WHERE s.app_id = rows.app_id
                AND s.snapshot_date = ?) AS name
        FROM rows GROUP BY app_id ORDER BY chart_rows DESC, best_rank, app_id
        """,
        [mechanic, d, d],
    )
    total_rows = _rows(con, "SELECT COUNT(*) AS n FROM stg_mr_chart WHERE snapshot_date = ?", [d])
    others = [r for r in sim if r["app_id"] != exclude]
    dev_rows: dict[str, int] = {}
    for r in sim:
        dev_rows[r["developer"] or "?"] = dev_rows.get(r["developer"] or "?", 0) + r["chart_rows"]
    all_rows = sum(dev_rows.values())
    top = sorted(dev_rows.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    out["saturation"] = {
        "similar_apps": len(others),
        "similar_in_grossing": sum(1 for r in others if r["grossing_rows"]),
        "chart_rows": all_rows,
        "list_rows_total": total_rows[0]["n"] if total_rows else None,
        "top_publishers": [{"developer": k, "chart_rows": v} for k, v in top],
        "top3_share": round(sum(v for _, v in top) / all_rows, 4) if all_rows else None,
        "examples": [
            {
                "app_id": r["app_id"],
                "name": r["name"],
                "developer": r["developer"],
                "best_rank": r["best_rank"],
            }
            for r in others[:8]
        ],
        "basis": "observed",
    }
    return out


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_iso)
