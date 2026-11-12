"""Data-trust gate: before explaining anything, is the data fit to explain?

Per app, from the warehouse's own bookkeeping:
  * freshness (fct_source_freshness): the GA4 source more than 7 days behind
    the as-of date blocks the app; more than 3 days (or any other dense
    source more than 3 days) is a warning;
  * data-quality checks (meta_check_results, latest run): a failed error-level
    check blocks; a failed warning is reported;
  * gaps: days without DAU between the first and last data day;
  * a volume drop: the last day's DAU below half the median of the 7 before
    (a broken export looks like an audience collapse);
  * sample size: median DAU and median install cohort over the last 14 days
    below 50 make every finding directional only; the jobs then report
    intervals but the narration says so.

"Twyman's law" (Kohavi et al., KDD 2017): an unexpected move is more often a
data problem than a real one, so the gate runs first and a blocked app gets a
data card instead of explanations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..evidence import Evidence, Insight, Recommendation
from . import common as C

GA4_SOURCES = ("ga4_files", "ga4_bigquery")
# Rows exist only when something happens (checks.yaml `sparse`): not judged by data date.
SPARSE = (
    "crashlytics_bigquery", "crashlytics_files", "revenuecat", "app_store", "play_reviews",
    "app_store_reviews", "meta_ads", "google_ads", "tiktok_ads", "ua_csv", "admob",
)  # fmt: skip
STALE_DAYS = 3
OLD_DAYS = 7
SMALL_N = 50
ZERO_RUN = 3  # consecutive zero-DAU days inside the active period


@dataclass
class Trust:
    app_id: str
    status: str  # ok | warn | block
    reasons: list[str] = field(default_factory=list)
    small: bool = False
    evidence: Evidence | None = None


def gate(ctx: C.Context) -> dict[str, Trust]:
    con, as_of = ctx.con, ctx.as_of
    out: dict[str, Trust] = {}
    fresh_cols, fresh = (
        C.query(
            con,
            "SELECT app_id, source, data_through, last_loaded_at FROM fct_source_freshness "
            "ORDER BY app_id, source",
        )
        if C.has_table(con, "fct_source_freshness")
        else ([], [])
    )
    failed = _failed_checks(ctx)
    for app in ctx.apps:
        t = Trust(app, "ok")
        facts = {"as_of": C.fact(as_of, "date"), "stale_after": C.fact(STALE_DAYS, "id")}
        facts["old_after"] = C.fact(OLD_DAYS, "id")
        stale: list[str] = []
        ga4_lag = None
        for a, source, through, _loaded in fresh:
            if a != app or through is None:
                continue
            lag = (as_of - through).days
            if source in GA4_SOURCES:
                ga4_lag = lag if ga4_lag is None else min(ga4_lag, lag)
                facts["data_through"] = C.fact(through, "date")
            elif source not in SPARSE and lag > STALE_DAYS:
                stale.append(source)
        if ga4_lag is not None:
            facts["lag_days"] = C.fact(ga4_lag, "days")
            if ga4_lag > OLD_DAYS:
                t.reasons.append("old_source")
            elif ga4_lag > STALE_DAYS:
                t.reasons.append("stale_source")
        if stale:
            t.reasons.append("stale_other")
            facts["stale_sources"] = C.fact(", ".join(stale), "text")
        app_failed = [c for c in failed if c["app_id"] in (None, app)]
        if any(c["severity"] == "error" for c in app_failed):
            t.reasons.append("failed_check")
        elif app_failed:
            t.reasons.append("failed_warning")
        if app_failed:
            facts["failed_checks"] = C.fact(len(app_failed), "count")
            facts["failed_check_names"] = C.fact(
                ", ".join(sorted({c["name"] for c in app_failed})), "text"
            )
        sql = (
            "WITH d AS (SELECT date, dau FROM fct_app_daily WHERE app_id = ? AND dau IS NOT NULL), "
            "b AS (SELECT MIN(date) AS lo, MAX(date) AS hi, COUNT(*) AS days FROM d) "
            "SELECT b.lo, b.hi, b.days, date_diff('day', b.lo, b.hi) + 1 - b.days AS missing_days, "
            "(SELECT median(dau) FROM d WHERE date > b.hi - 14) AS dau_median_14d, "
            "(SELECT dau FROM d WHERE date = b.hi) AS dau_last, "
            "(SELECT median(dau) FROM d WHERE date >= b.hi - 7 AND date < b.hi) AS dau_prior_median "
            "FROM b"
        )
        cols, rows = C.query(con, sql, [app]) if C.has_table(con, "fct_app_daily") else ([], [])
        row = dict(zip(cols, rows[0], strict=False)) if rows else {}
        if row.get("days"):
            facts["window_days"] = C.fact(row["days"], "count")
            facts["missing_days"] = C.fact(row["missing_days"], "count")
            facts["dau_median_14d"] = C.fact(
                row["dau_median_14d"], _median_unit(row["dau_median_14d"])
            )
            if (row["missing_days"] or 0) > 0:
                t.reasons.append("missing_days")
            last, prior = row.get("dau_last"), row.get("dau_prior_median")
            if last is not None and prior and prior >= SMALL_N and last < 0.5 * prior:
                t.reasons.append("volume_drop")
                facts["dau_last"] = C.fact(last, "count")
                facts["dau_prior_median"] = C.fact(prior, "count")
            zeros = _zero_runs(con, app)
            if zeros and zeros["longest"] >= ZERO_RUN:
                t.reasons.append("zero_days")
                facts.update(
                    zero_days=C.fact(zeros["days"], "count"),
                    zero_run=C.fact(zeros["longest"], "count"),
                    zero_from=C.fact(zeros["start"], "date"),
                    zero_to=C.fact(zeros["end"], "date"),
                )
            if (row.get("dau_median_14d") or 0) < SMALL_N:
                t.small = True
        csql = (
            "SELECT median(cohort_size) FROM fct_retention WHERE app_id = ? AND day_n = 0 "
            "AND cohort_date > (SELECT MAX(cohort_date) FROM fct_retention WHERE app_id = ?) - 14"
        )
        if C.has_table(con, "fct_retention"):
            _, crow = C.query(con, csql, [app, app])
            cohort_med = crow[0][0] if crow else None
            if cohort_med is not None:
                facts["cohort_median_14d"] = C.fact(cohort_med, _median_unit(cohort_med))
                if cohort_med < SMALL_N:
                    t.small = True
        if t.small:
            t.reasons.append("small_sample")
            facts["min_n"] = C.fact(SMALL_N, "count")
        if "old_source" in t.reasons or "failed_check" in t.reasons:
            t.status = "block"
        elif t.reasons:
            t.status = "warn"
        t.evidence = C.make_evidence(
            "data_trust",
            app,
            "gate",
            "data-trust gate: freshness, failed checks, gaps, volume and sample size",
            sql=sql,
            params=[app],
            columns=cols,
            rows=rows,
            facts=facts,
            caveats=list(t.reasons),
            as_of=as_of,
        )
        out[app] = t
    return out


def _median_unit(v: object) -> str:
    """A median of counts can end in .5: print it as is instead of rounding it to a count."""
    return "count" if isinstance(v, (int, float)) and float(v).is_integer() else "ratio"


def _zero_runs(con: object, app: str) -> dict[str, object] | None:
    """Days with zero DAU between the first and last active day, and the longest run of them.

    A run of several silent days inside an active period is either a real stop
    or a missing export day; the gate cannot tell which, so it names the dates.
    """
    _, rows = C.query(
        con,  # type: ignore[arg-type]
        "SELECT date, COALESCE(dau, 0) FROM fct_app_daily WHERE app_id = ? ORDER BY date",
        [app],
    )
    active = [d for d, n in rows if n > 0]
    if len(active) < 2:
        return None
    lo, hi = active[0], active[-1]
    best = {"days": 0, "longest": 0, "start": None, "end": None}
    run: list[object] = []
    for d, n in rows:
        if not lo < d < hi:
            continue
        if n == 0:
            best["days"] += 1  # type: ignore[operator]
            run.append(d)
            if len(run) > best["longest"]:  # type: ignore[operator]
                best.update(longest=len(run), start=run[0], end=run[-1])
        else:
            run = []
    return best


def _failed_checks(ctx: C.Context) -> list[dict[str, object]]:
    con = ctx.con
    if not C.has_table(con, "meta_check_results"):
        return []
    _, rows = C.query(
        con,
        "SELECT name, severity, sample FROM meta_check_results WHERE status <> 'pass' AND run_id = "
        "(SELECT run_id FROM meta_check_results ORDER BY checked_at DESC, run_id DESC LIMIT 1)",
    )
    out = []
    for name, severity, sample in rows:
        apps: set[str | None] = set()
        try:
            for r in json.loads(sample or "[]"):
                if isinstance(r, dict) and r.get("app_id"):
                    apps.add(str(r["app_id"]))
        except (ValueError, TypeError):
            pass
        for a in apps or {None}:
            out.append({"name": name, "severity": severity, "app_id": a})
    return out


def insights(trust: dict[str, Trust]) -> list[Insight]:
    out = []
    for t in trust.values():
        if t.status == "ok" or t.evidence is None:
            continue
        only_small = t.reasons == ["small_sample"]
        severity = "critical" if t.status == "block" else "info" if only_small else "warning"
        recs = [Recommendation("fix_data_source", [t.evidence.id])]
        if t.small:
            recs.append(Recommendation("small_sample_directional", [t.evidence.id]))
        out.append(
            Insight(
                id=f"data_trust:{t.app_id}",
                kind="data_trust",
                app_id=t.app_id,
                severity=severity,
                template="data_trust_block" if t.status == "block" else "data_trust_warn",
                evidence=[t.evidence],
                recommendations=recs if not only_small else recs[1:],
                blocked=t.status == "block",
                score=10.0 if t.status == "block" else 1.0,
            )
        )
    return out
