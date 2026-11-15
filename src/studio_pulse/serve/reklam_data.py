"""The numbers of the ad centre (/reklam), read from the warehouse per request.

Every figure comes from `stg_ua_spend` (network reports, one row per day x campaign x ad x country, USD) or the
cohort ROAS tables, with the same definitions as docs/METRICS.md (User acquisition, Creative scorecard):

  CPI         spend / network-reported installs
  CTR         clicks / impressions
  IPM         network installs / impressions x 1000
  view rate   video views / impressions, over the rows of networks that report views (Meta 3 s, TikTok 2 s)
  completion  completed views / views, over the same rows
  D0 / D7 ROAS  sum of the cohorts' revenue by day N / their spend, for the spend days of the period whose day N is
              complete (fct_ua_cohort_roas; the revenue basis is the network's, see stg_ua_cohort_cum)

A creative is `creative_id` when its ad name is creative-forge's (cf1.…), otherwise `network:ad_id`, as in
fct_creative_scorecard. Nothing here is specific to a game: every configured app with UA rows gets the same page.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

NETWORKS = ("meta", "google_ads", "tiktok", "applovin", "unity_ads", "mintegral", "ironsource", "moloco",
            "apple_search_ads", "other")  # fmt: skip
# colour slot per network (--s1..--s8): colour follows the network on every chart, never its rank
SLOT = {"meta": 1, "google_ads": 2, "tiktok": 3, "applovin": 4, "unity_ads": 5, "mintegral": 6, "ironsource": 7,
        "moloco": 8, "apple_search_ads": 7, "other": 8}  # fmt: skip
LABEL = {"meta": "Meta", "google_ads": "Google Ads", "tiktok": "TikTok", "applovin": "AppLovin",
         "unity_ads": "Unity Ads", "mintegral": "Mintegral", "ironsource": "ironSource", "moloco": "Moloco",
         "apple_search_ads": "Apple Search Ads", "other": "Diğer"}  # fmt: skip
# the config's source kind per network, and the variables it needs (see serve/dataflow.py)
SOURCE = {"meta": "meta_ads", "google_ads": "google_ads", "tiktok": "tiktok_ads", "applovin": "applovin_ads",
          "unity_ads": "unity_ads"}  # fmt: skip
VIEWS_REPORTED = (
    "meta",
    "tiktok",
)  # networks whose reports carry video views (others: CSV columns if present)
CREATIVE_KEY = "COALESCE(creative_id, network || ':' || COALESCE(ad_id, ad_name, '(none)'))"


def label(network: str) -> str:
    return LABEL.get(network, network)


def slot(network: str) -> int:
    return SLOT.get(network, 8)


# -- the period ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Period:
    key: str  # today | 7d | 30d | custom
    start: dt.date
    end: dt.date
    prev_start: dt.date
    prev_end: dt.date
    is_today: bool  # the end is the calendar day (live), not the last day with data

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def dates(self) -> list[dt.date]:
        return [self.start + dt.timedelta(days=i) for i in range(self.days)]


def period(
    key: str, last: dt.date | None, today: dt.date, frm: str | None = None, to: str | None = None
) -> Period:
    """`today`: the newest day with data (today itself once a refresh has read it); `7d` / `30d`: the days ending
    there; `custom`: from..to, clamped to 1..180 days. The previous period is the same length right before."""
    end = min(last or today, today)
    if key == "custom":
        try:
            a, b = dt.date.fromisoformat(str(frm)), dt.date.fromisoformat(str(to))
        except ValueError:
            key, a, b = "7d", end - dt.timedelta(days=6), end
        else:
            a, b = min(a, b), max(a, b)
            b = min(b, today)
            a = max(a, b - dt.timedelta(days=179))
        start, end = a, b
    else:
        n = {"today": 1, "30d": 30}.get(key, 7)
        key = key if key in ("today", "7d", "30d") else "7d"
        start = end - dt.timedelta(days=n - 1)
    length = (end - start).days + 1
    pe = start - dt.timedelta(days=1)
    return Period(key, start, end, pe - dt.timedelta(days=length - 1), pe, end == today)


# -- small helpers -----------------------------------------------------------------------------------------
def div(a: float | None, b: float | None, scale: float = 1.0) -> float | None:
    if a is None or not b:
        return None
    return a * scale / b


def metrics(r: dict[str, Any]) -> dict[str, Any]:
    """Derived rates of one aggregate row (spend, impressions, clicks, installs, views, completions, view_imps)."""
    out = dict(r)
    out["cpi"] = div(r.get("spend"), r.get("installs"))
    out["ctr"] = div(r.get("clicks"), r.get("impressions"))
    out["ipm"] = div(r.get("installs"), r.get("impressions"), 1000.0)
    out["view_rate"] = div(r.get("views"), r.get("view_imps"))
    out["completion"] = div(r.get("completions"), r.get("views"))
    return out


_AGG = """
    SUM(spend_usd) AS spend, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
    SUM(network_installs) AS installs, SUM(video_views) AS views, SUM(video_completions) AS completions,
    SUM(CASE WHEN video_views IS NOT NULL THEN impressions END) AS view_imps
"""


def last_day(rows: Any, con: Any, app_id: str) -> dt.date | None:
    r = rows(con, "SELECT MAX(date) AS d FROM stg_ua_spend WHERE app_id = ?", [app_id])
    return r[0]["d"] if r and r[0].get("d") else None


def has_ua(rows: Any, con: Any) -> set[str]:
    return {r["app_id"] for r in rows(con, "SELECT DISTINCT app_id FROM stg_ua_spend")}


def totals(rows: Any, con: Any, app_id: str, a: dt.date, b: dt.date, where: str = "", params: list | None = None,
           ) -> dict[str, Any]:  # fmt: skip
    r = rows(con, f"SELECT {_AGG} FROM stg_ua_spend WHERE app_id = ? AND date BETWEEN ? AND ? {where}",
             [app_id, a, b, *(params or [])])  # fmt: skip
    return metrics(r[0] if r else {})


def roas(
    rows: Any, con: Any, app_id: str, a: dt.date, b: dt.date, data_end: dt.date
) -> dict[str, Any]:
    """D0 over the period's spend days; D7 over those whose day 7 is complete (cohort day + 7 <= data end)."""
    r = rows(
        con,
        "SELECT SUM(revenue_d0_usd) AS r0, SUM(CASE WHEN revenue_d0_usd IS NOT NULL THEN spend_usd END) AS s0, "
        "SUM(CASE WHEN cohort_date <= ? THEN revenue_d7_usd END) AS r7, "
        "SUM(CASE WHEN cohort_date <= ? AND revenue_d7_usd IS NOT NULL THEN spend_usd END) AS s7, "
        "COUNT(DISTINCT CASE WHEN cohort_date <= ? AND revenue_d7_usd IS NOT NULL THEN cohort_date END) AS n7 "
        "FROM fct_ua_cohort_roas WHERE app_id = ? AND cohort_date BETWEEN ? AND ?",
        [data_end - dt.timedelta(days=7)] * 3 + [app_id, a, b],
    )
    x = r[0] if r else {}
    return {
        "d0": div(x.get("r0"), x.get("s0")),
        "d7": div(x.get("r7"), x.get("s7")),
        "d7_days": x.get("n7") or 0,
    }


def payback(rows: Any, con: Any, app_id: str) -> dict[str, Any]:
    r = rows(con, "SELECT payback_day, curve_days, roas_d30 FROM fct_ua_summary "
                  "WHERE app_id = ? AND network = '(all)'", [app_id])  # fmt: skip
    x = r[0] if r else {}
    return {
        "day": x.get("payback_day"),
        "curve_days": x.get("curve_days"),
        "roas_d30": x.get("roas_d30"),
    }


def daily(rows: Any, con: Any, app_id: str, a: dt.date, b: dt.date, by_network: bool = False,
          ) -> dict[Any, dict[dt.date, dict[str, Any]]]:  # fmt: skip
    """{network or '(all)': {date: metrics}} for every day of [a, b] (missing days: zero spend)."""
    grp = "network, date" if by_network else "date"
    sel = "network" if by_network else "'(all)' AS network"
    out: dict[Any, dict[dt.date, dict[str, Any]]] = defaultdict(dict)
    for r in rows(con, f"SELECT {sel}, date, {_AGG} FROM stg_ua_spend WHERE app_id = ? AND date BETWEEN ? AND ? "
                       f"GROUP BY {grp} ORDER BY {grp}", [app_id, a, b]):  # fmt: skip
        out[r["network"]][r["date"]] = metrics(r)
    return out


def cohort_daily(rows: Any, con: Any, app_id: str, a: dt.date, b: dt.date, data_end: dt.date,
                 ) -> dict[dt.date, dict[str, Any]]:  # fmt: skip
    out = {}
    for r in rows(
        con,
        "SELECT cohort_date AS date, SUM(revenue_d0_usd) / NULLIF(SUM(spend_usd), 0) AS d0, "
        "CASE WHEN cohort_date <= ? THEN SUM(revenue_d7_usd) / NULLIF(SUM(spend_usd), 0) END AS d7 "
        "FROM fct_ua_cohort_roas WHERE app_id = ? AND cohort_date BETWEEN ? AND ? GROUP BY cohort_date",
        [data_end - dt.timedelta(days=7), app_id, a, b],
    ):
        out[r["date"]] = r
    return out


# -- the campaign tree --------------------------------------------------------------------------------------
def tree(rows: Any, con: Any, app_id: str, p: Period) -> list[dict[str, Any]]:
    """network -> campaign -> creative, each with the period's metrics, the previous period's spend, a daily
    spend series for the sparkline and whether it delivered on the newest day."""
    base = (f"SELECT network, campaign_key, MAX(campaign_name) AS campaign_name, {CREATIVE_KEY} AS ckey, "
            f"MAX(creative_id) AS creative_id, MAX(ad_name) AS ad_name, date, {_AGG} "
            "FROM stg_ua_spend WHERE app_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY network, campaign_key, ckey, date")  # fmt: skip
    cur = rows(con, base, [app_id, p.start, p.end])
    prev = rows(con, base, [app_id, p.prev_start, p.prev_end])
    first = {r["ckey"]: r["d"] for r in rows(con, f"SELECT {CREATIVE_KEY} AS ckey, MIN(date) AS d FROM stg_ua_spend "
                                                  "WHERE app_id = ? GROUP BY ckey", [app_id])}  # fmt: skip
    dates = p.dates()
    nodes: dict[tuple, dict[str, Any]] = {}

    def node(key: tuple, level: str, **meta: Any) -> dict[str, Any]:
        if key not in nodes:
            nodes[key] = {"level": level, "key": "|".join(key), "children": [], "_days": defaultdict(float),
                          "_sum": defaultdict(float), "_prev": 0.0, "_last": None, **meta}  # fmt: skip
        return nodes[key]

    sums = ("spend", "impressions", "clicks", "installs", "views", "completions", "view_imps")
    for r in cur:
        n = node((r["network"],), "network", network=r["network"], name=label(r["network"]))
        c = node((r["network"], r["campaign_key"]), "campaign", network=r["network"], name=r["campaign_name"],
                 campaign_key=r["campaign_key"])  # fmt: skip
        k = node((r["network"], r["campaign_key"], r["ckey"]), "creative", network=r["network"],
                 name=r["ad_name"] or r["ckey"], creative_key=r["ckey"], creative_id=r["creative_id"],
                 campaign_name=r["campaign_name"], first_day=first.get(r["ckey"]))  # fmt: skip
        for x in (n, c, k):
            for f in sums:
                if r.get(f) is not None:
                    x["_sum"][f] += float(r[f])
            x["_days"][r["date"]] += float(r.get("spend") or 0)
            if (r.get("spend") or 0) > 0 or (r.get("impressions") or 0) > 0:
                x["_last"] = max(x["_last"] or r["date"], r["date"])
    for r in prev:
        for key in (
            (r["network"],),
            (r["network"], r["campaign_key"]),
            (r["network"], r["campaign_key"], r["ckey"]),
        ):
            if key in nodes:
                nodes[key]["_prev"] += float(r.get("spend") or 0)
    for key, x in nodes.items():
        m = metrics({f: x["_sum"].get(f) for f in sums})
        x.update(m)
        x["prev_spend"] = x.pop("_prev")
        x["series"] = [x["_days"].get(d, 0.0) for d in dates]
        x["live"] = x["_last"] == p.end
        x["last_delivery"] = x.pop("_last")
        x["days_live"] = (p.end - x["first_day"]).days + 1 if x.get("first_day") else None
        del x["_days"], x["_sum"]
        if len(key) > 1:
            nodes[key[:-1]]["children"].append(x)
    out = [x for k, x in nodes.items() if len(k) == 1]
    order = {n: i for i, n in enumerate(NETWORKS)}

    def sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items.sort(key=lambda x: (-(x.get("spend") or 0), str(x.get("name"))))
        for x in items:
            sort(x["children"])
        return items

    sort(out)
    out.sort(key=lambda x: (-(x.get("spend") or 0), order.get(x["network"], 99)))
    return out


def creative_detail(
    rows: Any, con: Any, app_id: str, ckey: str, p: Period
) -> dict[str, Any] | None:
    hist = rows(
        con,
        f"SELECT date, network, campaign_key, MAX(campaign_name) AS campaign_name, MAX(ad_name) AS ad_name, "
        f"MAX(creative_id) AS creative_id, {_AGG} FROM stg_ua_spend WHERE app_id = ? AND {CREATIVE_KEY} = ? "
        "GROUP BY date, network, campaign_key ORDER BY date",
        [app_id, ckey],
    )
    if not hist:
        return None
    by_day: dict[dt.date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    where: dict[tuple, dict[str, Any]] = {}
    sums = ("spend", "impressions", "clicks", "installs", "views", "completions", "view_imps")
    for r in hist:
        for f in sums:
            if r.get(f) is not None:
                by_day[r["date"]][f] += float(r[f])
        w = where.setdefault((r["network"], r["campaign_key"]), {"network": r["network"], "campaign": r["campaign_name"],
                                                                   "spend": 0.0, "first": r["date"], "last": r["date"],
                                                                   "in_period": 0.0})  # fmt: skip
        w["spend"] += float(r.get("spend") or 0)
        w["last"] = max(w["last"], r["date"])
        if p.start <= r["date"] <= p.end:
            w["in_period"] += float(r.get("spend") or 0)
    days = sorted(by_day)
    first, last = days[0], days[-1]
    series = [{"date": d, **metrics(dict(by_day[d]))} for d in days]
    total = metrics({f: sum(by_day[d].get(f, 0.0) for d in days) for f in sums})
    period_tot = totals(rows, con, app_id, p.start, p.end, f"AND {CREATIVE_KEY} = ?", [ckey])
    return {
        "key": ckey,
        "creative_id": hist[-1].get("creative_id"),
        "ad_name": hist[-1].get("ad_name"),
        "first": first,
        "last": last,
        "days_live": (last - first).days + 1,
        "series": series,
        "total": total,
        "period": period_tot,
        "where": sorted(where.values(), key=lambda w: -w["spend"]),
        "fatigue": fatigue_of(by_day, first),
    }


def fatigue_of(by_day: dict[dt.date, dict[str, float]], first: dt.date) -> dict[str, Any]:
    """IPM of the first 7 days live against the newest 7 (needs 14+ days live); a drop of 20% or more whose two
    Wilson-based proportions differ at p < 0.05 is fatigue."""
    from ..brain.stats import two_proportions

    days = sorted(by_day)
    if (days[-1] - first).days + 1 < 14:
        return {"state": "young", "days": (days[-1] - first).days + 1}

    def window(ds: list[dt.date]) -> tuple[float, float]:
        return (
            sum(by_day[d].get("installs", 0.0) for d in ds),
            sum(by_day[d].get("impressions", 0.0) for d in ds),
        )

    head = [d for d in days if d <= first + dt.timedelta(days=6)]
    tail = [d for d in days if d >= days[-1] - dt.timedelta(days=6)]
    (x1, n1), (x2, n2) = window(head), window(tail)
    if n1 < 2000 or n2 < 2000:
        return {"state": "thin", "imps": min(n1, n2)}
    t = two_proportions(x1, n1, x2, n2)
    ipm1, ipm2 = x1 / n1 * 1000, x2 / n2 * 1000
    change = ipm2 / ipm1 - 1 if ipm1 else None
    tired = t is not None and change is not None and change <= -0.2 and t.p_value < 0.05
    state = "tired" if tired else ("watch" if change is not None and change <= -0.1 else "ok")
    return {"state": state, "ipm_first": ipm1, "ipm_last": ipm2, "change": change,
            "p": t.p_value if t else None, "days": (days[-1] - first).days + 1}  # fmt: skip


def fatigue_curves(rows: Any, con: Any, app_id: str, p: Period, top: int = 5) -> dict[str, Any]:
    """IPM and CTR by days live (week buckets) for the period's biggest creatives with enough impressions."""
    big = rows(
        con,
        f"SELECT {CREATIVE_KEY} AS ckey, MAX(ad_name) AS ad_name, MAX(network) AS network, SUM(spend_usd) AS spend "
        "FROM stg_ua_spend WHERE app_id = ? AND date BETWEEN ? AND ? GROUP BY ckey HAVING SUM(impressions) >= 5000 "
        "ORDER BY spend DESC LIMIT ?",
        [app_id, p.start, p.end, top],
    )
    if not big:
        return {"weeks": [], "curves": []}
    keys = [b["ckey"] for b in big]
    data = rows(
        con,
        f"WITH s AS (SELECT {CREATIVE_KEY} AS ckey, date, impressions, clicks, network_installs FROM stg_ua_spend "
        "WHERE app_id = ?), f AS (SELECT ckey, MIN(date) AS d0 FROM s GROUP BY ckey) "
        "SELECT s.ckey, CAST(FLOOR(DATEDIFF('day', f.d0, s.date) / 7) AS INTEGER) AS week, "
        "SUM(s.network_installs) * 1000.0 / NULLIF(SUM(s.impressions), 0) AS ipm, "
        "CAST(SUM(s.clicks) AS DOUBLE) / NULLIF(SUM(s.impressions), 0) AS ctr, SUM(s.impressions) AS imps "
        "FROM s JOIN f USING (ckey) WHERE list_contains(?::VARCHAR[], s.ckey) GROUP BY 1, 2 ORDER BY 1, 2",
        [app_id, keys],
    )
    weeks = sorted({d["week"] for d in data if d["imps"] and d["imps"] >= 1000})[:12]
    curves = []
    for b in big:
        pts = {d["week"]: d for d in data if d["ckey"] == b["ckey"] and (d["imps"] or 0) >= 1000}
        curves.append({**b, "ipm": [pts[w]["ipm"] if w in pts else None for w in weeks],
                       "ctr": [pts[w]["ctr"] if w in pts else None for w in weeks]})  # fmt: skip
    return {"weeks": weeks, "curves": curves}


def network_split(rows: Any, con: Any, app_id: str, p: Period) -> list[dict[str, Any]]:
    r = rows(con, f"SELECT network, {_AGG} FROM stg_ua_spend WHERE app_id = ? AND date BETWEEN ? AND ? "
                  "GROUP BY network", [app_id, p.start, p.end])  # fmt: skip
    total = sum(float(x.get("spend") or 0) for x in r) or 0.0
    inst = sum(float(x.get("installs") or 0) for x in r) or 0.0
    out = []
    for x in r:
        m = metrics(x)
        m["spend_share"] = div(m.get("spend"), total)
        m["install_share"] = div(m.get("installs"), inst)
        out.append(m)
    out.sort(key=lambda m: -(m.get("spend") or 0))
    return out


def creatives_in(rows: Any, con: Any, app_id: str, a: dt.date, b: dt.date) -> list[dict[str, Any]]:
    """Per creative and network over [a, b]: counts for the AI summary's comparisons."""
    r = rows(con, f"SELECT network, {CREATIVE_KEY} AS ckey, MAX(ad_name) AS ad_name, MAX(creative_id) AS creative_id, "
                  f"{_AGG} FROM stg_ua_spend WHERE app_id = ? AND date BETWEEN ? AND ? GROUP BY network, ckey",
             [app_id, a, b])  # fmt: skip
    return [metrics(x) for x in r]


# -- freshness ---------------------------------------------------------------------------------------------
def freshness(rows: Any, con: Any, cfg: Any, app_id: str, today: dt.date) -> dict[str, Any]:
    """Per connected UA source: the newest day and the last load; per network not connected: its config key."""
    from .dataflow import missing_env

    app = next((a for a in cfg.apps if a.id == app_id), None)
    connected, rows_out = set(), []
    fresh = {r["source"]: r for r in rows(con, "SELECT source, data_through, last_loaded_at FROM fct_source_freshness "
                                               "WHERE app_id = ?", [app_id])}  # fmt: skip
    for skey, src in ((app.sources if app else {}) or {}).items():
        src = src or {}
        kind = skey.split("@", 1)[0]
        net = next((n for n, k in SOURCE.items() if k == kind), None)
        if kind == "ua_csv":
            net = str(src.get("network") or "other").lower()
        if net is None:
            continue
        connected.add(net)
        f = fresh.get(skey) or {}
        through = f.get("data_through")
        env = missing_env(kind, src) if kind != "ua_csv" else []
        loaded = f.get("last_loaded_at")
        rows_out.append({
            "network": net, "source": skey, "kind": kind, "through": through, "loaded": loaded,
            "loaded_iso": (loaded.replace(tzinfo=dt.UTC).isoformat() if isinstance(loaded, dt.datetime)
                           and loaded.tzinfo is None else (loaded.isoformat() if loaded else None)),
            "lag": (today - through).days if through else None, "csv": kind == "ua_csv",
            "replay": bool(src.get("replay")), "missing_env": env,
        })  # fmt: skip
    rows_out.sort(key=lambda r: NETWORKS.index(r["network"]) if r["network"] in NETWORKS else 99)
    missing = [
        n for n in ("meta", "google_ads", "tiktok", "applovin", "unity_ads") if n not in connected
    ]
    return {"rows": rows_out, "missing": missing}


def store_url(app: Any) -> str | None:
    """The app's public store listing from its config ids (Play package first, then the Apple id)."""
    import re

    pkgs, apple = [], []
    for src in (app.sources or {}).values():
        src = src or {}
        for k in ("package",):
            if src.get(k):
                pkgs.append(str(src[k]))
        for k in ("app_info_ids", "package_names", "bundle_ids"):
            pkgs += [str(x) for x in src.get(k) or []]
        if src.get("apple_id"):
            apple.append(str(src["apple_id"]))
    pkgs = [p for p in pkgs if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+", p)]
    if pkgs:
        return f"https://play.google.com/store/apps/details?id={pkgs[0]}"
    apple = [a for a in apple if a.isdigit() and 6 <= len(a) <= 12]
    return f"https://apps.apple.com/app/id{apple[0]}" if apple else None


def store_ids(app: Any) -> set[str]:
    from .dataflow import app_identifiers

    return app_identifiers(app)


def finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
