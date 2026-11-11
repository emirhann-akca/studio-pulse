"""Predicted LTV v1: curve fits of cumulative ARPU with a time-based backtest.

Input: fct_cohort_ltv (cumulative revenue per install cohort by day). Cohorts
are pooled into weekly groups (by the Monday of the install date) so a group
has enough users for a stable curve; a group's ARPU(N) is the pooled
cumulative revenue / pooled size of its cohorts, defined when all of them have
reached day N.

Two models are fitted by least squares on days 0..fit_days (default 7):
  power  ARPU(t) = a (t + 1)^b         (linear in log ARPU vs log(t + 1))
  log    ARPU(t) = a + b ln(t + 1)
and extrapolated. Curve shapes change little between neighbouring weeks, so
the extrapolation is calibrated: the prediction is multiplied by the median of
actual / predicted D30 over earlier groups whose D30 was already known when the
predicted group's day-7 data arrived (no look-ahead; factor 1 without such a
group). A prediction never falls below the observed day-7 value.

Backtest: every group with a known D30 is predicted that way in time order;
the absolute percentage errors give the MAPE of each model, and the model with
the lower MAPE is used. For the recent cohorts the calibration is the median
actual / raw D30 ratio of every group known today, and the interval is the
10th-90th percentile of those ratios (min-max with fewer than 5 groups),
widened to at least +-MAPE. These are model outputs, not data, and are
labelled so on the dashboard.

Known limit: a subscription app's curve rises in steps (trial conversion on day
7, renewals around day 37), which neither curve shape follows; its backtest
MAPE shows it (the demo's fitness app: about 40%). A subscription model
(shifted-beta-geometric, pymc-marketing) is the planned v2.

Tables: fct_pltv_backtest (one row per model and test group), fct_pltv_summary
(per app: chosen model, MAPE, predicted D30/D60/D90/D180 of the recent cohorts
with intervals) and fct_pltv_curve (per app and day 0..180: observed pooled
ARPU where known and the calibrated prediction with its interval).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any

FIT_DAYS = 7
TARGET_DAY = 30
HORIZONS = (14, 30, 60, 90, 180)
CURVE_DAYS = 180
MODELS = ("power", "log")
RECENT_COHORTS = 28


@dataclass
class Fit:
    model: str
    a: float
    b: float

    def predict(self, t: float) -> float:
        if self.model == "power":
            return self.a * (t + 1.0) ** self.b
        return self.a + self.b * math.log(t + 1.0)


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return my - b * mx, b


def fit(model: str, curve: dict[int, float], fit_days: int = FIT_DAYS) -> Fit | None:
    """Least-squares fit on days 0..fit_days of a cumulative ARPU curve {day: arpu}."""
    pts = [(t, v) for t, v in sorted(curve.items()) if t <= fit_days and v is not None]
    if model == "power":
        pts = [(t, v) for t, v in pts if v > 0]
        if len(pts) < 3:
            return None
        ln_a, b = _ols([math.log(t + 1.0) for t, _ in pts], [math.log(v) for _, v in pts])
        return Fit("power", math.exp(ln_a), b)
    if len(pts) < 3:
        return None
    a, b = _ols([math.log(t + 1.0) for t, _ in pts], [v for _, v in pts])
    return Fit("log", a, b)


@dataclass
class Group:
    key: dt.date  # Monday of the install week
    cohorts: int
    users: int
    curve: dict[int, float]  # day -> pooled cumulative ARPU (complete days only)
    last_cohort: dt.date


def groups_from_rows(rows: list[tuple[dt.date, int, int, float]]) -> list[Group]:
    """rows: (cohort_date, day_n, cohort_size, cum_revenue_usd) of one app."""
    by_cohort: dict[dt.date, dict[int, tuple[int, float]]] = defaultdict(dict)
    for cd, n, size, rev in rows:
        by_cohort[cd][int(n)] = (int(size), float(rev))
    weeks: dict[dt.date, list[dt.date]] = defaultdict(list)
    for cd in by_cohort:
        weeks[cd - dt.timedelta(days=cd.weekday())].append(cd)
    out = []
    for wk, cds in sorted(weeks.items()):
        cds.sort()
        days = set.intersection(*(set(by_cohort[c]) for c in cds))
        users = sum(by_cohort[c][0][0] for c in cds if 0 in by_cohort[c])
        curve = {}
        for n in sorted(days):
            size = sum(by_cohort[c][n][0] for c in cds)
            if size > 0:
                curve[n] = sum(by_cohort[c][n][1] for c in cds) / size
        out.append(Group(wk, len(cds), users, curve, cds[-1]))
    return out


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def backtest(
    groups: list[Group],
    model: str,
    fit_days: int = FIT_DAYS,
    target: int = TARGET_DAY,
    min_cohorts: int = 7,
) -> list[dict[str, Any]]:
    """Predict each group's D{target} from its days 0..fit_days, calibrated on earlier groups."""
    usable = [g for g in groups if g.cohorts >= min_cohorts and fit_days in g.curve]
    known: list[tuple[dt.date, float]] = []  # (date the target became known, actual/raw ratio)
    raw_fits: dict[dt.date, float] = {}
    for g in usable:
        f = fit(model, g.curve, fit_days)
        if f and target in g.curve:
            raw = f.predict(target)
            if raw > 0:
                raw_fits[g.key] = raw
                known.append((g.last_cohort + dt.timedelta(days=target), g.curve[target] / raw))
    out = []
    for g in usable:
        if target not in g.curve or g.key not in raw_fits:
            continue
        as_of = g.last_cohort + dt.timedelta(days=fit_days)
        train = [r for when, r in known if when <= as_of]
        factor = median(train) if train else 1.0
        pred = raw_fits[g.key] * factor
        actual = g.curve[target]
        out.append(
            {
                "model": model,
                "cohort_group": g.key,
                "cohorts": g.cohorts,
                "users": g.users,
                "fit_days": fit_days,
                "horizon_day": target,
                "train_groups": len(train),
                "calibration": factor,
                "predicted_arpu_usd": pred,
                "actual_arpu_usd": actual,
                "ape": abs(pred - actual) / actual if actual else None,
            }
        )
    return out


def mape(rows: list[dict[str, Any]]) -> float | None:
    errs = [r["ape"] for r in rows if r["ape"] is not None]
    return sum(errs) / len(errs) if errs else None


def recent_curve(
    rows: list[tuple[dt.date, int, int, float]],
    recent: int = RECENT_COHORTS,
    fit_days: int = FIT_DAYS,
) -> tuple[dict[int, float], int, int]:
    """Pooled curve of the latest `recent` cohorts that have reached fit_days."""
    eligible = sorted({cd for cd, n, _, _ in rows if n == fit_days})[-recent:]
    keep = set(eligible)
    acc: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for cd, n, size, rev in rows:
        if cd in keep:
            a = acc[int(n)]
            a[0] += rev
            a[1] += size
            a[2] += 1
    curve = {n: a[0] / a[1] for n, a in sorted(acc.items()) if a[2] == len(keep) and a[1] > 0}
    users = int(sum(size for cd, n, size, _ in rows if cd in keep and n == 0))
    return curve, len(keep), users


def _r(v: Any) -> Any:
    """9 significant digits: identical stored values on every OS (libm may differ in the last ulp)."""
    if isinstance(v, float) and math.isfinite(v):
        return float(f"{v:.9g}")
    return v


def predict_app(rows: list[tuple[dt.date, int, int, float]]) -> dict[str, Any] | None:
    groups = groups_from_rows(rows)
    tests = {m: backtest(groups, m) for m in MODELS}
    scores = {m: mape(t) for m, t in tests.items()}
    ranked = sorted((s, m) for m, s in scores.items() if s is not None)
    if not ranked:
        return None
    best = ranked[0][1]
    curve, n_cohorts, users = recent_curve(rows)
    f = fit(best, curve)
    if f is None:
        return None
    # Calibration of the recent pool: actual / raw D30 of every group whose D30 is known now.
    ratios = []
    for g in groups:
        gf = fit(best, g.curve)
        if g.cohorts >= 7 and gf and TARGET_DAY in g.curve and gf.predict(TARGET_DAY) > 0:
            ratios.append(g.curve[TARGET_DAY] / gf.predict(TARGET_DAY))
    factor = median(ratios) if ratios else 1.0
    err = scores[best] or 0.0
    if len(ratios) >= 5:
        lo_q, hi_q = _quantile(ratios, 0.1), _quantile(ratios, 0.9)
    elif ratios:
        lo_q, hi_q = min(ratios), max(ratios)
    else:
        lo_q = hi_q = factor
    # The interval is at least +-MAPE around the prediction: a model that backtests
    # badly cannot show a narrow band.
    lo_q, hi_q = min(lo_q, factor * (1 - err)), max(hi_q, factor * (1 + err))
    return {
        "model": best,
        "fit": f,
        "factor": factor,
        "mape": scores,
        "tests": tests,
        "curve": curve,
        "cohorts": n_cohorts,
        "users": users,
        "lo_ratio": max(0.0, lo_q),
        "hi_ratio": hi_q,
        "floor": curve.get(FIT_DAYS, 0.0),
    }


def predicted(res: dict[str, Any], t: int, ratio_key: str = "factor") -> float:
    """Calibrated prediction at day t; cumulative ARPU never falls below the observed day-7 value."""
    ratio = res[ratio_key]
    return max(res["fit"].predict(t) * ratio, res["floor"])


def store(con: Any) -> int:
    """(Re)build fct_pltv_backtest, fct_pltv_summary and fct_pltv_curve."""
    con.execute(
        "CREATE OR REPLACE TABLE fct_pltv_backtest (app_id VARCHAR, model VARCHAR, "
        "cohort_group DATE, cohorts INTEGER, users BIGINT, fit_days INTEGER, horizon_day INTEGER, "
        "train_groups INTEGER, calibration DOUBLE, predicted_arpu_usd DOUBLE, "
        "actual_arpu_usd DOUBLE, ape DOUBLE)"
    )
    con.execute(
        "CREATE OR REPLACE TABLE fct_pltv_summary (app_id VARCHAR, model VARCHAR, fit_days INTEGER, "
        "a DOUBLE, b DOUBLE, calibration DOUBLE, mape_power DOUBLE, mape_log DOUBLE, "
        "backtest_groups INTEGER, recent_cohorts INTEGER, recent_users BIGINT, "
        "observed_d7_usd DOUBLE, horizon_day INTEGER, predicted_arpu_usd DOUBLE, "
        "low_usd DOUBLE, high_usd DOUBLE)"
    )
    con.execute(
        "CREATE OR REPLACE TABLE fct_pltv_curve (app_id VARCHAR, day_n INTEGER, model VARCHAR, "
        "observed_arpu_usd DOUBLE, predicted_arpu_usd DOUBLE, low_usd DOUBLE, high_usd DOUBLE)"
    )
    try:
        data = con.execute(
            "SELECT app_id, cohort_date, day_n, cohort_size, cum_revenue_usd FROM fct_cohort_ltv "
            "ORDER BY 1, 2, 3"
        ).fetchall()
    except Exception:
        return 0
    by_app: dict[str, list[tuple[dt.date, int, int, float]]] = defaultdict(list)
    for app_id, cd, n, size, rev in data:
        by_app[app_id].append((cd, int(n), int(size), float(rev or 0.0)))
    n_apps = 0
    for app_id, rows in sorted(by_app.items()):
        res = predict_app(rows)
        if res is None:
            continue
        n_apps += 1
        for m in MODELS:
            for r in res["tests"][m]:
                con.execute(
                    "INSERT INTO fct_pltv_backtest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        _r(v)
                        for v in (
                            app_id,
                            r["model"],
                            r["cohort_group"],
                            r["cohorts"],
                            r["users"],
                            r["fit_days"],
                            r["horizon_day"],
                            r["train_groups"],
                            r["calibration"],
                            r["predicted_arpu_usd"],
                            r["actual_arpu_usd"],
                            r["ape"],
                        )
                    ],
                )
        f, k = res["fit"], res["factor"]
        for h in HORIZONS:
            p = predicted(res, h)
            con.execute(
                "INSERT INTO fct_pltv_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    _r(v)
                    for v in (
                        app_id,
                        res["model"],
                        FIT_DAYS,
                        f.a,
                        f.b,
                        k,
                        res["mape"]["power"],
                        res["mape"]["log"],
                        len(res["tests"][res["model"]]),
                        res["cohorts"],
                        res["users"],
                        res["curve"].get(FIT_DAYS),
                        h,
                        p,
                        predicted(res, h, "lo_ratio"),
                        predicted(res, h, "hi_ratio"),
                    )
                ],
            )
        for t in range(CURVE_DAYS + 1):
            obs = res["curve"].get(t)
            future = t > FIT_DAYS
            con.execute(
                "INSERT INTO fct_pltv_curve VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    _r(v)
                    for v in (
                        app_id,
                        t,
                        res["model"],
                        obs,
                        predicted(res, t) if future else obs,
                        predicted(res, t, "lo_ratio") if future else obs,
                        predicted(res, t, "hi_ratio") if future else obs,
                    )
                ],
            )
    return n_apps
