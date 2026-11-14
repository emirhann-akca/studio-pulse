"""Why did a metric move? Driver analysis for "why" questions.

The change is the current period against the one before it: the last 7
days against the 7 before for daily metrics, the newest 7 complete install
cohorts against the 14 before for retention. It is then split by the
dimensions the warehouse has for that metric, and the segment that moved it
most is named with its contribution:

  * D1 / D7 / D30 retention: insights.cohorts.explain - Kitagawa mix/rate,
    Adtributor and DIFF-lite (brain/attribution.py) over platform, country,
    app version, first-touch source and device tier (the cohort_dims pull);
  * ratio metrics with a platform split (crash-free users and sessions,
    sessions per DAU): the exact Kitagawa split by platform;
  * additive metrics: each segment's share of the change - platform for
    activity (stg_active_users), IAP vs ads for revenue, network / country /
    platform for UA spend and installs (stg_ua_spend); CPI as spend over
    installs, split by network;
  * frame time and memory: the device tier that moved most (fct_perf_daily),
    then the newest app version of that tier (fct_perf_by_version);
  * a release that first appeared inside the current period is named for
    every metric, because tables such as the crash counts carry no version.

Every answer ends with the repository's caveat: an association in time, not
a measured cause (an A/B test measures a cause).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from . import attribution as A
from .catalog import Catalog, Metric
from .evidence import Evidence, Fact

REST = "__rest__"  # seg0_label: the rest of the same cohorts (worded per language)
REST_TEXT = {"en": "the rest of the same cohorts", "tr": "aynı kohortların geri kalanı"}
RETENTION_N = {"d1_retention": 1, "d7_retention": 7, "d30_retention": 30}
PLATFORM_RATIO = {
    # metric -> (x, n) over stg_active_users: rate = x / n
    "crash_free_users": ("dau - LEAST(crashed_users, dau)", "dau"),
    "crash_free_sessions": ("sessions - LEAST(crashed_sessions, sessions)", "sessions"),
    "sessions_per_dau": ("sessions", "dau"),
    "engagement_min_per_dau": ("engagement_sec / 60.0", "dau"),
}
PLATFORM_ADDITIVE = {
    "dau": "dau",
    "new_users": "new_users",
    "sessions": "sessions",
    "crashed_users": "crashed_users",
}
UA_ADDITIVE = {"ua_spend_usd": "spend_usd", "ua_installs": "network_installs"}
REVENUE_PARTS = {"revenue_usd": ("iap_revenue_usd", "ad_revenue_usd")}
PERF = {
    "frame_time_p50_ms": "ft_p50_ms",
    "frame_time_p95_ms": "ft_p95_ms",
    "memory_p95_mb": "mem_alloc_p95_mb",
    "slow_frame_share": "slow_frame_share",
}
SEGMENT_TEXT = {
    "en": {"platform": "platform", "country": "country", "app_version": "app version",
           "network": "network", "device_tier": "device tier", "component": "revenue type"},
    "tr": {"platform": "platform", "country": "ülke", "app_version": "uygulama sürümü",
           "network": "ağ", "device_tier": "cihaz sınıfı", "component": "gelir türü"},
}  # fmt: skip
COMPONENT_NAMES = {
    "iap_revenue_usd": {"en": "in-app purchases", "tr": "uygulama içi satın alma"},
    "ad_revenue_usd": {"en": "ads", "tr": "reklam"},
}


@dataclass
class Result:
    metric: Metric
    app: str
    value0: float | None
    value1: float | None
    unit: str
    current: tuple[dt.date, dt.date]
    baseline: tuple[dt.date, dt.date]
    dimension: str | None = None
    segment: str | None = None
    contribution: float | None = None
    ep: float | None = None
    seg0: float | None = None
    seg1: float | None = None
    seg0_label: str | None = None  # the segment the "before" value belongs to (previous version)
    release: tuple[str, dt.date] | None = None
    method: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    note: str | None = None  # "no_split": the metric has no dimension to split by


def _q(session: Any, sql: str, params: list[Any]) -> tuple[list[str], list[list[Any]]]:
    res = session.run_trusted(sql, params)
    return res.columns, [list(r) for r in res.rows]


def _ev(kind: str, app: str, method: str, sql: str, params: list[Any], cols: list[str],
        rows: list[list[Any]], facts: dict[str, Fact]) -> Evidence:  # fmt: skip
    return Evidence(
        id=f"why:{kind}:{app}", kind="answer", app_id=app, method=method, sql=sql,
        params=list(params), columns=cols, rows=[list(r) for r in rows[:200]], facts=facts,
        caveats=["association_in_time"],
    )  # fmt: skip


def _last_day(session: Any, model: str, col: str, app: str, where: str = "TRUE") -> dt.date | None:
    _, rows = _q(session, f"SELECT MAX({col}) FROM {model} WHERE app_id = ? AND {where}", [app])
    return rows[0][0] if rows and rows[0][0] else None


def _windows(
    last: dt.date, cur_days: int, base_days: int
) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    cur = (last - dt.timedelta(days=cur_days - 1), last)
    base = (cur[0] - dt.timedelta(days=base_days), cur[0] - dt.timedelta(days=1))
    return cur, base


def release_in(
    session: Any, schema: dict[str, Any], app: str, period: tuple[dt.date, dt.date]
) -> tuple[str, dt.date] | None:
    """The app version first seen inside the period (newest first), from any table with versions."""
    best: tuple[str, dt.date] | None = None
    for sql in (
        "SELECT app_version, MIN(first_seen) FROM fct_perf_by_version WHERE app_id = ? GROUP BY 1",
        "SELECT app_version, MIN(cohort_date) FROM stg_cohort_dims WHERE app_id = ? AND app_version IS NOT NULL GROUP BY 1",
        "SELECT app_version, MIN(first_date) FROM fct_rating_by_version WHERE app_id = ? GROUP BY 1",
    ):
        table = sql.split(" FROM ")[1].split()[0]
        if table not in schema:
            continue
        try:
            _, rows = _q(session, sql, [app])
        except Exception:
            continue
        firsts = {str(v): d for v, d in rows if v and d}
        if not firsts:
            continue
        window_start = min(firsts.values())
        for v, d in firsts.items():
            if d == window_start:
                continue  # present from the first day of the data: not a release inside it
            if period[0] <= d <= period[1] and (best is None or d > best[1]):
                best = (v, d)
        if best:
            return best
    return best


def _retention(session: Any, ctx: Any, metric: Metric, app: str) -> Result | None:
    from .insights import cohorts

    n = RETENTION_N[metric.id]
    last = _last_day(session, "fct_retention", "cohort_date", app, f"day_n = {n} AND is_complete")
    if last is None:
        return None
    cur, base = _windows(last, 7, 14)
    expl = cohorts.explain(ctx, app, n, cur, base)
    if expl is None:
        return None
    attr = expl.evidence[-1]
    dec = expl.evidence[0]
    f = attr.facts if "r0" in attr.facts else dec.facts
    r = Result(metric, app, f["r0"].value, f["r1"].value, "pct", cur, base,
               method="Kitagawa mix/rate + Adtributor + DIFF-lite over the cohort_dims pull",
               evidence=list(expl.evidence))  # fmt: skip
    if expl.cause is not None:
        c = expl.cause
        r.dimension = c.dimension.split(",")[0] if "," not in c.dimension else c.dimension
        r.segment = c.value
        r.seg1 = c.rate
        r.seg0 = c.rest_rate  # the rest of the same cohorts
        r.seg0_label = REST
        row = next((x for x in (dec.rows or []) if x and x[0] == c.value), None)
        if row is not None:
            r.contribution, r.ep = row[7], row[8]
    else:
        rows = [x for x in dec.rows if x[0] != A.OTHER]
        if rows:
            top = rows[0]  # decompose() sorts by contribution in the direction of the change
            r.dimension = str(dec.facts["dimension"].value)
            r.segment, r.seg0, r.seg1, r.contribution, r.ep = top[0], top[3], top[4], top[7], top[8]
    return r


def _split_rows(
    session: Any, sql: str, params: list[Any]
) -> list[tuple[str, float, float, float, float]]:
    """(segment, n0, x0, n1, x1) from a query returning segment, period, n, x."""
    _, rows = _q(session, sql, params)
    acc: dict[str, list[float]] = {}
    for seg, period, n, x in rows:
        a = acc.setdefault(str(seg), [0.0, 0.0, 0.0, 0.0])
        k = 2 if period == 1 else 0
        a[k] += float(n or 0)
        a[k + 1] += float(x or 0)
    return [(s, *v) for s, v in sorted(acc.items())]  # type: ignore[misc]


def _ratio(session: Any, metric: Metric, app: str, dim: str, model: str, x: str, n: str,
           last: dt.date, scale: float = 1.0) -> Result | None:  # fmt: skip
    cur, base = _windows(last, 7, 7)
    sql = (
        f"SELECT {dim}, CASE WHEN date >= ? THEN 1 ELSE 0 END AS period, SUM({n}), SUM({x}) "
        f"FROM {model} WHERE app_id = ? AND date BETWEEN ? AND ? AND {dim} IS NOT NULL GROUP BY 1, 2"
    )
    params = [cur[0], app, base[0], cur[1]]
    segs = _split_rows(session, sql, params)
    if not segs:
        return None
    tot = A.Totals(
        sum(s[1] for s in segs),
        sum(s[2] for s in segs),
        sum(s[3] for s in segs),
        sum(s[4] for s in segs),
    )
    if not tot.n0 or not tot.n1:
        return None
    dec = A.decompose(dim, [A.Element(*s) for s in segs], tot)
    top = dec.rows[0]
    cols = ["value", "share0", "share1", "rate0", "rate1", "contribution", "ep"]
    rows = [[r.value, r.share0, r.share1, r.rate0, r.rate1, r.contribution, r.ep] for r in dec.rows]
    res = Result(metric, app, tot.r0 * scale, tot.r1 * scale, metric.unit, cur, base, dim,
                 top.value, top.contribution * scale, top.ep,
                 (top.rate0 or 0) * scale, (top.rate1 or 0) * scale,
                 method=f"Kitagawa split of {metric.id} by {dim}")  # fmt: skip
    res.evidence.append(_ev(metric.id, app, res.method, sql, params, cols, rows, {}))
    return res


def _additive(session: Any, metric: Metric, app: str, dim: str, model: str, col: str,
              last: dt.date, mean: bool) -> Result | None:  # fmt: skip
    cur, base = _windows(last, 7, 7)
    agg = f"SUM({col})"
    sql = (
        f"SELECT {dim}, CASE WHEN date >= ? THEN 1 ELSE 0 END AS period, COUNT(DISTINCT date), {agg} "
        f"FROM {model} WHERE app_id = ? AND date BETWEEN ? AND ? GROUP BY 1, 2"
    )
    params = [cur[0], app, base[0], cur[1]]
    segs = _split_rows(session, sql, params)
    if not segs:
        return None
    days0, days1 = 7.0, 7.0
    rows = []
    for s, _d0, x0, _d1, x1 in segs:
        v0 = x0 / days0 if mean else x0
        v1 = x1 / days1 if mean else x1
        rows.append([s, v0, v1, v1 - v0])
    t0 = sum(r[1] for r in rows)
    t1 = sum(r[2] for r in rows)
    delta = t1 - t0
    rows.sort(key=lambda r: r[3] if delta < 0 else -r[3])
    top = rows[0]
    res = Result(metric, app, t0, t1, metric.unit, cur, base, dim, str(top[0]), top[3],
                 top[3] / delta if delta else None, top[1], top[2],
                 method=f"{'daily mean' if mean else 'sum'} of {metric.id} by {dim}, each segment's share of the change")  # fmt: skip
    res.evidence.append(
        _ev(
            metric.id,
            app,
            res.method,
            sql,
            params,
            ["value", "before", "after", "change"],
            rows,
            {},
        )
    )
    return res


def _revenue(session: Any, metric: Metric, app: str, last: dt.date) -> Result | None:
    cur, base = _windows(last, 7, 7)
    sql = (
        "SELECT CASE WHEN date >= ? THEN 1 ELSE 0 END AS period, SUM(iap_revenue_usd), "
        "SUM(ad_revenue_usd) FROM fct_app_daily WHERE app_id = ? AND date BETWEEN ? AND ? GROUP BY 1"
    )
    params = [cur[0], app, base[0], cur[1]]
    _, got = _q(session, sql, params)
    per = {int(r[0]): (float(r[1] or 0), float(r[2] or 0)) for r in got}
    if 0 not in per or 1 not in per:
        return None
    rows = [
        ["iap_revenue_usd", per[0][0], per[1][0], per[1][0] - per[0][0]],
        ["ad_revenue_usd", per[0][1], per[1][1], per[1][1] - per[0][1]],
    ]
    t0, t1 = sum(per[0]), sum(per[1])
    delta = t1 - t0
    rows.sort(key=lambda r: r[3] if delta < 0 else -r[3])
    top = rows[0]
    res = Result(metric, app, t0, t1, "usd", cur, base, "component", top[0], top[3],
                 top[3] / delta if delta else None, top[1], top[2],
                 method="net revenue split into IAP and ad revenue")  # fmt: skip
    res.evidence.append(
        _ev(
            metric.id,
            app,
            res.method,
            sql,
            params,
            ["value", "before", "after", "change"],
            rows,
            {},
        )
    )
    return res


def _perf(session: Any, metric: Metric, app: str, tier: str | None) -> Result | None:
    col = PERF[metric.id]
    last = _last_day(session, "fct_perf_daily", "date", app)
    if last is None:
        return None
    cur, base = _windows(last, 7, 7)
    sql = (
        f"SELECT device_tier, CASE WHEN date >= ? THEN 1 ELSE 0 END AS period, AVG({col}) "
        "FROM fct_perf_daily WHERE app_id = ? AND date BETWEEN ? AND ? AND device_tier <> 'all' "
        "GROUP BY 1, 2"
    )
    params = [cur[0], app, base[0], cur[1]]
    _, got = _q(session, sql, params)
    tiers: dict[str, list[float | None]] = {}
    for t, period, v in got:
        tiers.setdefault(str(t), [None, None])[int(period)] = v
    rows = [
        [t, a, b, (b - a) if a is not None and b is not None else None]
        for t, (a, b) in sorted(tiers.items())
    ]
    rows = [r for r in rows if r[3] is not None]
    if not rows:
        return None
    if tier:
        rows = [r for r in rows if r[0] == tier] or rows
    rows.sort(key=lambda r: -r[3])
    top = rows[0]
    res = Result(metric, app, top[1], top[2], metric.unit, cur, base, "device_tier", top[0],
                 top[3], None, top[1], top[2],
                 method=f"daily {col} by device tier, then by app version within that tier")  # fmt: skip
    res.evidence.append(
        _ev(
            metric.id,
            app,
            res.method,
            sql,
            params,
            ["value", "before", "after", "change"],
            rows,
            {},
        )
    )
    vcol = {"ft_p50_ms": "ft_p50_ms", "ft_p95_ms": "ft_p95_ms"}.get(col)
    if vcol:
        vsql = (
            f"SELECT app_version, first_seen, windows, {vcol} FROM fct_perf_by_version "
            "WHERE app_id = ? AND device_tier = ? ORDER BY first_seen DESC"
        )
        _, vrows = _q(session, vsql, [app, top[0]])
        if (
            len(vrows) >= 2
            and vrows[0][3] is not None
            and vrows[1][3] is not None
            and vrows[0][3] > vrows[1][3]
        ):
            res.dimension = "app_version"
            res.segment = f"{vrows[0][0]} ({top[0]} tier)"
            res.seg0_label = str(vrows[1][0])
            res.seg0, res.seg1 = vrows[1][3], vrows[0][3]
            res.contribution = None  # the version gap is in the sentence; not a share
            res.evidence.append(_ev(f"{metric.id}:version", app, "per version and tier (fct_perf_by_version)",
                                    vsql, [app, top[0]], ["app_version", "first_seen", "windows", vcol], vrows, {}))  # fmt: skip
    return res


def explain(session: Any, catalog: Catalog, metric: Metric, app: str, *, as_of: dt.date,
            tier: str | None = None) -> Result | None:  # fmt: skip
    """The driver of the newest change in `metric` for `app`, or None when it cannot be split."""
    schema = session.schema() if hasattr(session, "schema") else {}
    from .insights import common as C

    res: Result | None = None
    if metric.id in RETENTION_N and "stg_cohort_dims" in _tables(session):
        ctx = C.Context(con=session.con, as_of=as_of, catalog=catalog, apps=[app])
        res = _retention(session, ctx, metric, app)
    elif metric.id in PERF:
        res = _perf(session, metric, app, tier)
    else:
        last = _last_day(session, "fct_app_daily", "date", app, "dau IS NOT NULL")
        if last is None:
            return None
        if metric.id in PLATFORM_RATIO:
            x, n = PLATFORM_RATIO[metric.id]
            res = _ratio(session, metric, app, "platform", "stg_active_users", x, n, last)
        elif metric.id in PLATFORM_ADDITIVE:
            res = _additive(
                session,
                metric,
                app,
                "platform",
                "stg_active_users",
                PLATFORM_ADDITIVE[metric.id],
                last,
                metric.aggregation == "mean_of_daily",
            )
        elif metric.id in REVENUE_PARTS:
            res = _revenue(session, metric, app, last)
        elif metric.id in UA_ADDITIVE:
            ua_last = _last_day(session, "stg_ua_spend", "date", app)
            if ua_last:
                res = _additive(
                    session,
                    metric,
                    app,
                    "network",
                    "stg_ua_spend",
                    UA_ADDITIVE[metric.id],
                    ua_last,
                    False,
                )
        elif metric.id == "ua_cpi_usd":
            ua_last = _last_day(session, "stg_ua_spend", "date", app)
            if ua_last:
                res = _ratio(
                    session,
                    metric,
                    app,
                    "network",
                    "stg_ua_spend",
                    "spend_usd",
                    "network_installs",
                    ua_last,
                )
    if res is None:
        return None
    res.release = release_in(session, _tables(session) or schema, app, res.current)
    return res


def _tables(session: Any) -> dict[str, Any]:
    try:
        rows = session.run_trusted(
            "SELECT DISTINCT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).rows
    except Exception:
        return {}
    return {r[0]: True for r in rows}


TEXTS = {
    "head": {
        "en": "{metric} of {app}: {value1} for {cur_start} to {cur_end} against {value0} for {base_start} to {base_end} ({direction}).",
        "tr": "{app} {metric}: {cur_start} ile {cur_end} arası {value1}, {base_start} ile {base_end} arası {value0} ({direction}).",
    },
    "segment": {
        "en": " Largest contributor: {dim} = {segment}[[ ({seg0} before, {seg1} after)]][[, {contribution} of the change]][[ ({ep} of it)]].",
        "tr": " En büyük katkı: {dim} = {segment}[[ (önce {seg0}, sonra {seg1})]][[, değişimin {contribution} kadarı]][[ (tamamının {ep} kadarı)]].",
    },
    "release": {
        "en": " Version {version} first appeared on {first_seen}, inside this period.",
        "tr": " {version} sürümü ilk kez {first_seen} tarihinde, bu dönemin içinde görüldü.",
    },
    "release_only": {
        "en": " The {metric} tables carry no version or other split; version {version} first appeared on {first_seen}, inside this period.",
        "tr": " {metric} tablolarında sürüm ya da başka bir kırılım yok; {version} sürümü ilk kez {first_seen} tarihinde, bu dönemin içinde görüldü.",
    },
    "caveat": {
        "en": " Association in time: the split shows where the change is, not what caused it; an A/B test measures a cause.",
        "tr": " Zamansal ilişki: kırılım değişimin nerede olduğunu gösterir, neyin yol açtığını değil; nedeni bir A/B testi ölçer.",
    },
    "segment_vs": {
        "en": " Largest contributor: {dim} = {segment} at {seg1} against {seg0} for {seg0_label}[[; it accounts for {contribution} of the change]][[ ({ep} of it)]].",
        "tr": " En büyük katkı: {dim} = {segment}, {seg0_label} için {seg0} değerine karşı {seg1}[[; değişimin {contribution} kadarı bundan]][[ (tamamının {ep} kadarı)]].",
    },
    "not_moved": {
        "en": " Note: it moved the other way.",
        "tr": " Not: değişim sorulan yönün tersine.",
    },
}
_DOWN = re.compile(
    r"\b(drop\w*|fall\w*|fell|declin\w*|decreas\w*|down|lower|dus\w*|azal\w*|geriledi)\b"
)
_UP = re.compile(r"\b(rise|rose|risen|grow\w*|increas\w*|up|higher|jump\w*|art\w*|yuksel\w*)\b")
_WORSE = re.compile(r"\b(worse\w*|kotules\w*|bozul\w*)\b")
_BETTER = re.compile(r"\b(better|improv\w*|iyiles\w*)\b")


def asked_direction(norm: str, better: str) -> str | None:
    """ "up" / "down": the direction a why-question takes for granted, if it names one."""
    if _WORSE.search(norm):
        return "down" if better == "up" else "up" if better == "down" else None
    if _BETTER.search(norm):
        return "up" if better == "up" else "down" if better == "down" else None
    if _DOWN.search(norm):
        return "down"
    if _UP.search(norm):
        return "up"
    return None


DIRECTION = {
    "en": ("up {change}", "down {change}", "flat"),
    "tr": ("{change} arttı", "{change} düştü", "yatay"),
}
