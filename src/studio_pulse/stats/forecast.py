"""Seasonality-aware expected bands for the `forecast_band` alert condition.

A daily KPI of a mobile app has a weekly rhythm (weekends differ), a trend and
noise. A trailing-mean baseline mixes weekdays and weekends, so a rule loose
enough not to fire every weekend misses real drops. `forecast_band` instead
forecasts the evaluated day from the history before it, with weekly
seasonality, and fires when the actual value leaves the prediction interval.

Two engines:

  statsforecast   Nixtla statsforecast's AutoETS with season_length 7 and its
                  `level` prediction interval (optional extra
                  `studio-pulse[forecast]`, Apache-2.0).
  seasonal        dependency-free (the default without the extra): a least-
                  squares regression of log(value) on a linear trend and
                  weekday effects over the history, one robust refit without
                  points 4 robust SDs off, and the classical prediction
                  interval t(n - 8) s sqrt(1 + leverage); at least
                  `min_band_pct` (2%) wide.

Holidays (`holidays:` in the rule or its defaults) are known one-off days: a
rule never fires on them and they are left out of the history (the
statsforecast engine sees the seasonal expectation in their place), so a
holiday dip neither alerts nor distorts the following days.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from statistics import median

from .dist import t_ppf

ENGINES = ("auto", "seasonal", "statsforecast")
# Default prediction-interval level per engine. Measured on 120-day synthetic
# series (tests/test_forecast.py, seeds 1-8): the regression engine gives 0-1
# false alerts at 99.5%; AutoETS's analytic intervals are narrower and give
# 1-3 at 99.5% but 0-2 at 99.9%, hence its higher default.
DEFAULT_LEVEL = {"seasonal": 99.5, "statsforecast": 99.9}


@dataclass
class Band:
    center: float
    low: float
    high: float
    engine: str
    points: int


def statsforecast_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("statsforecast") is not None


def _daily(history: list[tuple[dt.date, float]], holidays: set[dt.date]) -> dict[dt.date, float]:
    return {d: v for d, v in history if d not in holidays and v is not None}


def _solve_inverse(m: list[list[float]]) -> list[list[float]] | None:
    """Inverse of a small symmetric matrix by Gauss-Jordan elimination with pivoting."""
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        p = a[col][col]
        a[col] = [x / p for x in a[col]]
        for r in range(n):
            if r != col and a[r][col] != 0.0:
                k = a[r][col]
                a[r] = [x - k * y for x, y in zip(a[r], a[col], strict=True)]
    return [row[n:] for row in a]


def _design(d: dt.date, t0: dt.date) -> list[float]:
    wd = d.weekday()
    return [1.0, (d - t0).days / 7.0] + [1.0 if wd == k else 0.0 for k in range(1, 7)]


def _ols(rows: list[tuple[list[float], float]]) -> tuple[list[float], list[list[float]]] | None:
    p = len(rows[0][0])
    xtx = [[sum(x[i] * x[j] for x, _ in rows) for j in range(p)] for i in range(p)]
    inv = _solve_inverse(xtx)
    if inv is None:
        return None
    xty = [sum(x[i] * y for x, y in rows) for i in range(p)]
    beta = [sum(inv[i][j] * xty[j] for j in range(p)) for i in range(p)]
    return beta, inv


def seasonal_band(
    history: list[tuple[dt.date, float]],
    target: dt.date,
    *,
    level: float = 95.0,
    holidays: set[dt.date] | None = None,
    min_points: int = 28,
    min_band_pct: float = 2.0,
) -> Band | None:
    """Prediction interval of a regression with a linear trend and weekday effects.

    log(value) = a + b t + weekday effects (additive values when any value is
    <= 0), fitted by least squares on the history; points more than 4 robust
    standard deviations off the first fit are dropped and the model refitted
    (a past incident does not widen every later band). The interval is
    t(n - 8) x s x sqrt(1 + x0' (X'X)^-1 x0), the classical prediction interval.
    """
    h = _daily([(d, v) for d, v in history if d < target], holidays or set())
    if len(h) < min_points:
        return None
    use_log = all(v > 0 for v in h.values())
    t0 = min(h)
    rows = [(_design(d, t0), math.log(v) if use_log else v) for d, v in sorted(h.items())]
    p = len(rows[0][0])
    fit = _ols(rows)
    if fit is None or len(rows) <= p + 2:
        return None
    beta, inv = fit
    resid = [y - sum(b * x for b, x in zip(beta, xs, strict=True)) for xs, y in rows]
    mad = 1.4826 * median(abs(r) for r in resid)
    if mad > 0:
        kept = [row for row, r in zip(rows, resid, strict=True) if abs(r) <= 4 * mad]
        if len(kept) > p + 2 and len(kept) < len(rows):
            fit = _ols(kept)
            if fit is None:
                return None
            beta, inv = fit
            rows = kept
            resid = [y - sum(b * x for b, x in zip(beta, xs, strict=True)) for xs, y in rows]
    dfree = len(rows) - p
    s2 = sum(r * r for r in resid) / dfree
    x0 = _design(target, t0)
    lev = sum(x0[i] * inv[i][j] * x0[j] for i in range(p) for j in range(p))
    half = t_ppf(0.5 + level / 200.0, dfree) * math.sqrt(s2 * (1.0 + lev))
    mean = sum(b * x for b, x in zip(beta, x0, strict=True))
    if use_log:
        center, low, high = math.exp(mean), math.exp(mean - half), math.exp(mean + half)
    else:
        center, low, high = mean, mean - half, mean + half
    floor = abs(center) * min_band_pct / 100.0
    low, high = min(low, center - floor), max(high, center + floor)
    return Band(center, low, high, "seasonal", len(rows))


def statsforecast_band(
    history: list[tuple[dt.date, float]],
    target: dt.date,
    *,
    level: float = 95.0,
    holidays: set[dt.date] | None = None,
    min_points: int = 28,
) -> Band | None:
    import numpy as np
    from statsforecast.models import AutoETS

    hol = holidays or set()
    pts = sorted((d, v) for d, v in history if d < target and v is not None)
    if len(pts) < min_points:
        return None
    first, last = pts[0][0], pts[-1][0]
    by_day = dict(pts)
    series = []
    d = first
    while d <= last:  # a regular daily series: gaps and holidays get the seasonal expectation
        v = by_day.get(d)
        if v is None or d in hol:
            b = seasonal_band(pts, d, level=level, holidays=hol, min_points=min(min_points, 14))
            v = (
                b.center
                if b
                else (series[-7] if len(series) >= 7 else (series[-1] if series else 0.0))
            )
        series.append(float(v))
        d += dt.timedelta(days=1)
    h = (target - last).days
    res = AutoETS(season_length=7).forecast(y=np.array(series, dtype=float), h=h, level=[level])
    key = int(level) if float(level).is_integer() else level
    return Band(
        float(res["mean"][-1]),
        float(res[f"lo-{key}"][-1]),
        float(res[f"hi-{key}"][-1]),
        "statsforecast",
        len(pts),
    )


def resolve_engine(engine: str) -> str:
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}")
    if engine == "statsforecast" and not statsforecast_available():
        raise ValueError('engine statsforecast needs: pip install "studio-pulse[forecast]"')
    if engine == "auto":
        return "statsforecast" if statsforecast_available() else "seasonal"
    return engine


def band(
    history: list[tuple[dt.date, float]],
    target: dt.date,
    *,
    level: float | None = None,
    engine: str = "auto",
    holidays: set[dt.date] | None = None,
    min_points: int = 28,
) -> Band | None:
    eng = resolve_engine(engine)
    lvl = float(level) if level is not None else DEFAULT_LEVEL[eng]
    if eng == "statsforecast":
        return statsforecast_band(
            history, target, level=lvl, holidays=holidays, min_points=min_points
        )
    return seasonal_band(history, target, level=lvl, holidays=holidays, min_points=min_points)


def outside(value: float, b: Band, direction: str = "both") -> bool:
    if direction == "drop":
        return value < b.low
    if direction == "rise":
        return value > b.high
    return value < b.low or value > b.high


def backtest(
    series: list[tuple[dt.date, float]],
    *,
    level: float | None = None,
    engine: str = "auto",
    holidays: set[dt.date] | None = None,
    history_days: int = 56,
    min_points: int = 28,
    direction: str = "both",
) -> list[dt.date]:
    """Evaluate every day as if it were the latest; return the days that would alert."""
    hol = holidays or set()
    fired = []
    for i, (d, v) in enumerate(series):
        if d in hol:
            continue
        lo = d - dt.timedelta(days=history_days)
        hist = [(x, y) for x, y in series[:i] if x >= lo]
        b = band(hist, d, level=level, engine=engine, holidays=hol, min_points=min_points)
        if b is not None and outside(v, b, direction):
            fired.append(d)
    return fired
