"""Release regressions: frame time by version x device tier, crashes since a release.

Frame time: the newest version against the previous one, per device tier,
on the perf windows of the same days (users who had not updated yet are the
control, so a weekday or content change does not pass for a regression).
Test: Mann-Whitney U on the per-window median frame times; effect: the
relative change of the medians with order-statistic 95% intervals. Reported
from +5% with q <= 0.10. GPU and CPU main-thread p95 are shown beside it: a
GPU p95 close to the frame p95 means GPU-bound.

Crashes: crash data carries no version here (Crashlytics daily counts, Play
vitals), so the comparison is in time: crashed users / DAU from the newest
version's first day against the 28 days before (two-proportion z on
user-days). The narration says "since version X appeared", not "caused by".
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .. import stats
from ..evidence import Insight, Recommendation
from . import common as C

MIN_WINDOWS = 30
MIN_REL = 0.05
CRASH_MIN_REL = 0.5
CRASH_MIN_PP = 0.001
BEFORE_DAYS = 28
AFTER_MAX_DAYS = 14

VERSIONS_SQL = """SELECT app_version, MIN(date) AS first_seen, COUNT(*) AS windows
FROM stg_perf_windows WHERE app_id = ? AND app_version IS NOT NULL AND ft_p50_ms IS NOT NULL
GROUP BY 1 ORDER BY first_seen, app_version"""

WINDOWS_SQL = """SELECT app_version, device_tier, ft_p50_ms, gpu_p95_ms, cpu_main_p95_ms, ft_p95_ms
FROM stg_perf_windows
WHERE app_id = ? AND app_version IN (?, ?) AND ft_p50_ms IS NOT NULL AND date >= ?"""

BY_VERSION_SQL = """SELECT app_version, device_tier, first_seen, windows, ft_p50_ms, ft_p95_ms,
       cpu_main_p95_ms, gpu_p95_ms, slow_frame_share
FROM fct_perf_by_version WHERE app_id = ? AND app_version IN (?, ?) ORDER BY device_tier, first_seen"""

CRASH_SQL = """SELECT CASE WHEN date >= ? THEN 1 ELSE 0 END AS after,
       SUM(dau) AS user_days, SUM(LEAST(crashed_users, dau)) AS crashed_user_days,
       COUNT(*) AS days, MIN(date) AS first_day, MAX(date) AS last_day
FROM fct_app_daily
WHERE app_id = ? AND crashed_users IS NOT NULL AND dau > 0 AND date >= ? AND date <= ?
GROUP BY 1 ORDER BY 1"""

VITALS_SQL = """SELECT CASE WHEN date >= ? THEN 1 ELSE 0 END AS after,
       SUM(distinct_users) AS users, SUM(user_perceived_crash_rate * distinct_users) AS crashed
FROM fct_android_vitals_daily
WHERE app_id = ? AND user_perceived_crash_rate IS NOT NULL AND date >= ? AND date <= ?
GROUP BY 1 ORDER BY 1"""


def _versions(ctx: C.Context, app: str) -> list[list[Any]]:
    if not C.has_table(ctx.con, "stg_perf_windows"):
        return []
    return C.query(ctx.con, VERSIONS_SQL, [app])[1]


def _median_of(rows: list[list[Any]], version: str, tier: str, k: int) -> float | None:
    vals = [w[k] for w in rows if w[0] == version and w[1] == tier and w[k] is not None]
    return stats.median(vals) if vals else None


def perf(ctx: C.Context) -> list[C.Candidate]:
    out: list[C.Candidate] = []
    for app in ctx.apps:
        if ctx.blocked(app):
            continue
        vers = [v for v in _versions(ctx, app) if (v[2] or 0) >= MIN_WINDOWS]
        if len(vers) < 2:
            continue
        new, new_day = vers[-1][0], vers[-1][1]
        old = max(vers[:-1], key=lambda v: v[2])[0]
        params = [app, new, old, new_day]
        _, wins = C.query(ctx.con, WINDOWS_SQL, params)
        if sum(1 for w in wins if w[0] == old) < MIN_WINDOWS:  # too few side by side: use all
            params = [app, new, old, dt.date(1970, 1, 1)]
            _, wins = C.query(ctx.con, WINDOWS_SQL, params)
        tiers = sorted({w[1] for w in wins})
        bv_cols, bv_rows = C.query(ctx.con, BY_VERSION_SQL, [app, new, old])
        for tier in tiers:
            a = [w[2] for w in wins if w[1] == tier and w[0] == old]
            b = [w[2] for w in wins if w[1] == tier and w[0] == new]
            if len(a) < MIN_WINDOWS or len(b) < MIN_WINDOWS:
                continue
            _, p = stats.mann_whitney(a, b)
            m_old, m_new = stats.median(a), stats.median(b)
            rel = m_new / m_old - 1 if m_old else 0.0
            if rel < MIN_REL:
                out.append(C.Candidate(p, None))
                continue
            ci_old, ci_new = stats.median_ci(a), stats.median_ci(b)

            gpu_new = _median_of(wins, new, tier, 3)
            ft95_new = _median_of(wins, new, tier, 5)
            facts: dict[str, Any] = {
                "version_new": C.fact(new, "version"),
                "version_old": C.fact(old, "version"),
                "tier": C.fact(tier, "text"),
                "ft_new": C.fact(m_new, "ms"),
                "ft_old": C.fact(m_old, "ms"),
                "rel": C.fact(rel, "pct"),
                "windows_new": C.fact(len(b), "count"),
                "windows_old": C.fact(len(a), "count"),
                "since": C.fact(new_day, "date"),
                "ci_level": C.fact(95, "id"),
            }
            if ci_new:
                facts.update(ft_new_lo=C.fact(ci_new[0], "ms"), ft_new_hi=C.fact(ci_new[1], "ms"))
            if ci_old:
                facts.update(ft_old_lo=C.fact(ci_old[0], "ms"), ft_old_hi=C.fact(ci_old[1], "ms"))
            gpu_bound = bool(gpu_new and ft95_new and gpu_new >= 0.85 * ft95_new)
            if gpu_new and ft95_new:
                facts.update(gpu_p95=C.fact(gpu_new, "ms"), ft_p95=C.fact(ft95_new, "ms"))
            ev = C.make_evidence(
                "perf_regression",
                app,
                f"{new}:{tier}",
                "Mann-Whitney U on per-window median frame time, newest vs previous version, "
                "same days; medians with order-statistic 95% intervals",
                sql=BY_VERSION_SQL,
                params=[app, new, old],
                columns=bv_cols,
                rows=[r for r in bv_rows if r[1] == tier],
                facts=facts,
                n=len(a) + len(b),
                effect=rel,
                effect_unit="pct",
                p=p,
                metric="frame_time_p50_ms",
                definition="median over perf windows of the per-window median frame time",
                caveats=["gpu_bound"] if gpu_bound else [],
                as_of=ctx.as_of,
            )
            out.append(
                C.Candidate(
                    p,
                    Insight(
                        id=f"perf_regression:{app}:{new}:{tier}",
                        kind="perf_regression",
                        app_id=app,
                        severity="warning",
                        template="perf_regression",
                        evidence=[ev],
                        recommendations=[Recommendation("rollback_or_fix_version", [ev.id])],
                        score=rel * 10,
                        p=p,
                    ),
                )
            )
    return out


def _release_day(ctx: C.Context, app: str) -> tuple[str | None, dt.date | None]:
    """The newest version and its first day (earliest sighting across the version tables)."""
    first: dict[str, dt.date] = {}
    for sql in (
        "SELECT app_version, MIN(first_seen) FROM fct_perf_by_version WHERE app_id = ? GROUP BY 1",
        "SELECT app_version, MIN(first_date) FROM fct_rating_by_version WHERE app_id = ? "
        "AND app_version IS NOT NULL GROUP BY 1",
    ):
        try:
            _, rows = C.query(ctx.con, sql, [app])
        except Exception:  # table absent in a partial warehouse
            continue
        for v, d in rows:
            if d is not None and (v not in first or d < first[v]):
                first[v] = d
    if not first:
        return None, None
    v = max(first, key=lambda k: (first[k], k))
    return v, first[v]


def crashes(ctx: C.Context) -> list[C.Candidate]:
    out: list[C.Candidate] = []
    if not C.has_table(ctx.con, "fct_app_daily"):
        return out
    for app in ctx.apps:
        if ctx.blocked(app):
            continue
        _, last = C.query(
            ctx.con,
            "SELECT MAX(date) FROM fct_app_daily WHERE app_id = ? AND crashed_users IS NOT NULL",
            [app],
        )
        last_day = last[0][0] if last else None
        if last_day is None:
            continue
        version, rel_day = _release_day(ctx, app)
        if rel_day is None or (last_day - rel_day).days > AFTER_MAX_DAYS or rel_day > last_day:
            version, rel_day = None, last_day - dt.timedelta(days=6)
        start = rel_day - dt.timedelta(days=BEFORE_DAYS)
        params = [rel_day, app, start, last_day]
        cols, rows = C.query(ctx.con, CRASH_SQL, params)
        per = {r[0]: r for r in rows}
        if 0 not in per or 1 not in per:
            continue
        b, a = per[0], per[1]
        t = stats.two_proportions(b[2] or 0, b[1] or 0, a[2] or 0, a[1] or 0)
        if t is None:
            continue
        worse = (
            t.diff > 0
            and t.p1 >= 0
            and t.diff >= CRASH_MIN_PP
            and (t.p1 == 0 or t.p2 / t.p1 - 1 >= CRASH_MIN_REL)
        )
        if not worse:
            out.append(C.Candidate(t.p_value, None))
            continue
        lo0, hi0 = stats.wilson(b[2] or 0, b[1] or 0)
        lo1, hi1 = stats.wilson(a[2] or 0, a[1] or 0)
        facts: dict[str, Any] = {
            "crash_before": C.fact(t.p1, "pct"),
            "crash_after": C.fact(t.p2, "pct"),
            "crash_before_lo": C.fact(lo0, "pct"),
            "crash_before_hi": C.fact(hi0, "pct"),
            "crash_after_lo": C.fact(lo1, "pct"),
            "crash_after_hi": C.fact(hi1, "pct"),
            "crash_free_after": C.fact(1 - t.p2, "pct"),
            "ratio": C.fact(t.p2 / t.p1 if t.p1 else None, "ratio"),
            "since": C.fact(rel_day, "date"),
            "days_after": C.fact(a[3], "count"),
            "days_before": C.fact(b[3], "count"),
            "user_days_after": C.fact(a[1], "count"),
            "ci_level": C.fact(95, "id"),
        }
        if version:
            facts["version"] = C.fact(version, "version")
        evidence = [
            C.make_evidence(
                "crash_regression",
                app,
                rel_day,
                "crashed users / DAU (user-days) from the newest version's first day vs the 28 "
                "days before; two-proportion z, Wilson 95% intervals",
                sql=CRASH_SQL,
                params=params,
                columns=cols,
                rows=rows,
                facts=facts,
                n=(a[1] or 0) + (b[1] or 0),
                effect=t.diff,
                effect_unit="pp",
                ci=t.ci,
                p=t.p_value,
                metric="crash_free_users",
                aggregation="pooled",
                caveats=["association_in_time", "user_days"],
                as_of=ctx.as_of,
            )
        ]
        if C.has_table(ctx.con, "fct_android_vitals_daily"):
            vparams = [rel_day, app, start, last_day]
            vcols, vrows = C.query(ctx.con, VITALS_SQL, vparams)
            vper = {r[0]: r for r in vrows}
            if 0 in vper and 1 in vper and vper[0][1] and vper[1][1]:
                vb, va = vper[0], vper[1]
                evidence.append(
                    C.make_evidence(
                        "vitals_crash",
                        app,
                        rel_day,
                        "Play vitals user-perceived crash rate, pooled by distinct users, same periods",
                        sql=VITALS_SQL,
                        params=vparams,
                        columns=vcols,
                        rows=vrows,
                        facts={
                            "vitals_before": C.fact(vb[2] / vb[1], "pct"),
                            "vitals_after": C.fact(va[2] / va[1], "pct"),
                            "play_bad": C.fact(0.0109, "pct"),
                        },
                        metric="vitals_crash_rate",
                        aggregation="pooled",
                        as_of=ctx.as_of,
                    )
                )
        sev = "critical" if 1 - t.p2 < 0.99 else "warning"
        out.append(
            C.Candidate(
                t.p_value,
                Insight(
                    id=f"crash_regression:{app}",
                    kind="crash_regression",
                    app_id=app,
                    severity=sev,
                    template="crash_regression_version" if version else "crash_regression",
                    evidence=evidence,
                    recommendations=[
                        Recommendation(
                            "rollback_or_fix_version" if version else "investigate_crashes",
                            [e.id for e in evidence],
                        )
                    ],
                    causal=False,
                    score=min(10.0, (t.p2 / t.p1 if t.p1 else 10.0)),
                    p=t.p_value,
                ),
            )
        )
    return out
