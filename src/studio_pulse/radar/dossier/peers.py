"""What the game does right RELATIVE to its genre peers, with numbers from the warehouse.

Peers: the games the radar tagged with the same mechanic (any profile: LLM or keyword) that are
in today's charts; with fewer than three, the games of the target's best chart today. For the
target and each peer, from the radar's tables of the latest snapshot day:

  best rank today and its list          fct_mr_breakout_daily (OBSERVED)
  days since the current version        raw_mr_app Lookup currentVersionReleaseDate (OBSERVED)
  ratings per day                       stg_mr_rating_daily via the breakout row (OBSERVED; only
                                        the storefronts where a game is charted, so the line
                                        names the target's)
  Play installs per day                 stg_mr_install_daily via the breakout row (STORE-REPORTED)
  countries charted (7 days)            fct_mr_breakout_daily spread (OBSERVED)
  age since release                     fct_mr_breakout_daily (OBSERVED, context only)
  store monetisation model              Play containsAds / offersIAP (STORE-REPORTED, context)

A metric becomes a strength when the target is at least 25 % better than the peers' median
(ranks: at least 25 % lower) and the gap is not trivial (ABS_MIN: 3 days, 5 ranks, ...), a
weakness when it is as much worse; every line carries the target value,
the median, n and the peers' ids. Review themes are compared with the peers' Play reviews when
those were mined (same LLM labelling, quotes checked): a complaint the target draws at least
15 points less often than its peers is a strength.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

import duckdb

from ..digest import fmt

MARGIN = 0.25
THEME_POINTS = 0.15
# the smallest difference worth a line: 2 days against 3 is 33 % but means nothing
ABS_MIN = {
    "best_rank": 5,
    "days_since_update": 3,
    "ratings_per_day": 5,
    "installs_per_day": 500,
    "countries": 1,
    "rating_avg": 0.2,
}


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    try:
        cur = con.execute(sql, params)
    except duckdb.CatalogException:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def latest_day(con: duckdb.DuckDBPyConnection, day: dt.date) -> dt.date | None:
    r = _rows(
        con,
        "SELECT MAX(snapshot_date) AS d FROM fct_mr_breakout_daily WHERE snapshot_date <= ?",
        [day],
    )
    return r[0]["d"] if r else None


def peer_ids(
    con: duckdb.DuckDBPyConnection, target: str, mechanic: str | None, day: dt.date
) -> tuple[list[str], str]:
    if mechanic:
        ids = [
            r["app_id"]
            for r in _rows(
                con,
                "SELECT DISTINCT b.app_id FROM fct_mr_breakout_daily AS b JOIN fct_mr_app_tags AS t "
                "ON t.app_key = b.app_key AND t.dimension IN ('mechanic_primary', 'mechanic') "
                "AND t.value = ? WHERE b.snapshot_date = ? AND b.app_id <> ? ORDER BY 1",
                [mechanic, day, target],
            )
        ]
        if len(ids) >= 3:
            return ids, "mechanic"
    best = _rows(
        con,
        "SELECT best_list FROM fct_mr_breakout_daily WHERE app_id = ? AND snapshot_date = ?",
        [target, day],
    )
    if not best:
        return [], "none"
    ids = [
        r["app_id"]
        for r in _rows(
            con,
            "SELECT DISTINCT c.app_id FROM stg_mr_chart AS c JOIN fct_mr_breakout_daily AS b "
            "ON b.app_id = c.app_id AND b.snapshot_date = c.snapshot_date "
            "WHERE c.snapshot_date = ? AND c.list_key = ? AND c.app_id <> ? ORDER BY 1",
            [day, best[0]["best_list"], target],
        )
    ]
    return ids, "list"


def metrics(
    con: duckdb.DuckDBPyConnection, ids: list[str], day: dt.date
) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    rows = _rows(
        con,
        """
        WITH b AS (
          SELECT app_id, name, developer, best_rank, best_list, age_days, rv_max, rv_country,
                 iv, spread, play_id
          FROM fct_mr_breakout_daily WHERE snapshot_date = ? AND app_id IN (SELECT UNNEST(?))
        ), v AS (
          SELECT app_id, MAX(current_version_date) AS cur, MAX(rating_avg) AS rating_avg
          FROM raw_mr_app WHERE store = 'apple' AND source = 'lookup' AND snapshot_date = (
            SELECT MAX(snapshot_date) FROM raw_mr_app WHERE store = 'apple' AND snapshot_date <= ?)
          GROUP BY app_id
        ), p AS (
          SELECT app_id, contains_ads, offers_iap FROM raw_mr_app WHERE store = 'play'
          QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC) = 1
        )
        SELECT b.*, v.cur, v.rating_avg, p.contains_ads, p.offers_iap
        FROM b LEFT JOIN v USING (app_id) LEFT JOIN p ON p.app_id = b.play_id
        """,
        [day, ids, day],
    )
    out = {}
    for r in rows:
        ads, iap = r["contains_ads"], r["offers_iap"]
        out[r["app_id"]] = {
            "app_id": r["app_id"],
            "name": r["name"],
            "developer": r["developer"],
            "best_rank": r["best_rank"],
            "best_list": r["best_list"],
            "age_days": r["age_days"],
            "ratings_per_day": None if r["rv_max"] is None else round(r["rv_max"], 1),
            "ratings_country": r["rv_country"],
            "installs_per_day": None if r["iv"] is None else round(r["iv"], 1),
            "countries": r["spread"],
            "days_since_update": (day - r["cur"]).days if r["cur"] else None,
            "rating_avg": None if r["rating_avg"] is None else round(r["rating_avg"], 2),
            "model": None
            if ads is None and iap is None
            else ("hybrid" if ads and iap else "iap" if iap else "ads" if ads else "free"),
        }
    return out


# metric -> (higher is better, basis, Turkish label, English label, decimals)
METRICS = {
    "best_rank": (False, "observed", "en iyi sıra", "best rank", 0),
    "days_since_update": (
        False,
        "observed",
        "son güncellemeden bu yana gün",
        "days since the last update",
        0,
    ),
    "ratings_per_day": (True, "observed", "günlük puan", "ratings per day", 1),
    "installs_per_day": (True, "store_reported", "Play günlük kurulum", "Play installs per day", 0),
    "countries": (True, "observed", "listede olduğu ülke (7 gün)", "countries charted (7 days)", 0),
    "rating_avg": (True, "observed", "puan ortalaması", "average rating", 2),
}


def _cmp(key: str, t: float, med: float) -> bool | None:
    higher, *_ = METRICS[key]
    if med is None or t is None or abs(t - med) < ABS_MIN[key]:
        return None
    if key == "rating_avg":  # ratings live in a narrow band: compare by 0.2 stars
        return True if t - med >= 0.2 else False if med - t >= 0.2 else None
    if higher:
        if t >= med * (1 + MARGIN) and t > med:
            return True
        if t <= med * (1 - MARGIN) and t < med:
            return False
        return None
    if t <= med * (1 - MARGIN) and t < med:
        return True
    if t >= med * (1 + MARGIN) and t > med:
        return False
    return None


def compare(
    con: duckdb.DuckDBPyConnection, target: str | None, mechanic: str | None, day: dt.date
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "none", "basis": "observed", "items": [], "peers": []}
    if not target:
        return out
    d = latest_day(con, day)
    if d is None:
        return out
    ids, how = peer_ids(con, target, mechanic, d)
    m = metrics(con, [target, *ids], d)
    me = m.get(target)
    peers = [m[i] for i in ids if i in m]
    if not me or not peers:
        return out
    out.update({"status": "ok", "day": d.isoformat(), "peer_set": how, "mechanic": mechanic})
    out["peers"] = sorted(peers, key=lambda p: (p["best_rank"] or 999, p["app_id"]))[:12]
    out["n_peers"] = len(peers)
    for key, (_higher, basis, tr, en, dec) in METRICS.items():
        vals = [p[key] for p in peers if p.get(key) is not None]
        if me.get(key) is None or len(vals) < 3:
            continue
        med = statistics.median(vals)
        better = _cmp(key, me[key], med)
        tv, mv = me[key], med
        mdec = dec if float(mv).is_integer() else max(dec, 1)  # a median of 32.5 stays 32.5
        # ratings/day come from the storefronts where each game is charted: name the target's
        where = (
            f" ({str(me.get('ratings_country') or '').upper()})"
            if key == "ratings_per_day" and me.get("ratings_country")
            else ""
        )
        out["items"].append(
            {
                "metric": key,
                "value": tv,
                "peer_median": round(mv, mdec) if isinstance(mv, float) else mv,
                "n": len(vals),
                "better": better,
                "basis": basis,
                "text": {
                    "tr": f"{tr}: bu oyun {fmt(tv, 'tr', dec)}{where}, akranların medyanı {fmt(mv, 'tr', mdec)} (n={len(vals)})",
                    "en": f"{en}: this game {fmt(tv, 'en', dec)}{where}, peers' median {fmt(mv, 'en', mdec)} (n={len(vals)})",
                },
            }
        )
    models = [p["model"] for p in peers if p.get("model")]
    if models and me.get("model"):
        same = sum(1 for x in models if x == me["model"])
        out["items"].append(
            {
                "metric": "model",
                "value": me["model"],
                "peer_share_same": round(same / len(models), 3),
                "n": len(models),
                "better": None,
                "basis": "store_reported",
                "text": {
                    "tr": f"para modeli {me['model']}; akranlarda aynı modeli kullananların payı %{fmt(100 * same / len(models), 'tr', 0)} (n={len(models)})",
                    "en": f"monetisation model {me['model']}; {fmt(100 * same / len(models), 'en', 0)}% of peers use the same (n={len(models)})",
                },
            }
        )
    out["age"] = {
        "value": me.get("age_days"),
        "peer_median": statistics.median(
            [p["age_days"] for p in peers if p.get("age_days") is not None] or [0]
        ),
        "basis": "observed",
    }
    return out


def compare_themes(
    target: dict[str, list[dict[str, Any]]],
    n_target: int,
    peers: dict[str, list[dict[str, Any]]],
    n_peers: int,
    themes: dict[str, Any],
) -> list[dict[str, Any]]:
    """Theme shares of the target's reviews against the peers' pooled reviews (LLM labels)."""
    out = []
    if not n_target or not n_peers:
        return out
    for side in ("complaints", "praise"):
        t_c = {r["theme"]: r["count"] for r in target.get(side) or []}
        p_c = {r["theme"]: r["count"] for r in peers.get(side) or []}
        for theme in sorted(set(t_c) | set(p_c)):
            a, b = t_c.get(theme, 0), p_c.get(theme, 0)
            sa, sb = a / n_target, b / n_peers
            if max(a, b) < 2 or abs(sa - sb) < THEME_POINTS:
                continue
            good = sa < sb if side == "complaints" else sa > sb
            label = themes.get(theme, {"tr": theme, "en": theme})
            word_tr = "şikâyet" if side == "complaints" else "övgü"
            word_en = "complaints about" if side == "complaints" else "praise for"
            out.append(
                {
                    "metric": f"review_{side}:{theme}",
                    "value": a,
                    "of": n_target,
                    "peer_value": b,
                    "peer_of": n_peers,
                    "better": good,
                    "basis": "llm",
                    "text": {
                        "tr": f"{label['tr']} {word_tr}: bu oyunda {a}/{n_target} yorum (%{fmt(100 * sa, 'tr', 0)}), akranlarda {b}/{n_peers} (%{fmt(100 * sb, 'tr', 0)})",
                        "en": f"{word_en} {label['en']}: {a}/{n_target} reviews here ({fmt(100 * sa, 'en', 0)}%), {b}/{n_peers} for peers ({fmt(100 * sb, 'en', 0)}%)",
                    },
                }
            )
    return out
