"""forecast_band: a seasonal backtest with a planted anomaly and a holiday dip.

120 days of a DAU-like series: weekly rhythm (weekends -20%), +0.2%/day trend,
3% noise. Day 70 (a Wednesday) is a listed holiday at -25%; from day 100 the
series steps down 30%. forecast_band must catch the step on its first day,
stay silent on the holiday and the days after it, and raise at most one false
alert before the step. A trailing 14-day mean rule (drop 15%) on the same
series is counted alongside: it fires on ordinary weekends.
"""

from __future__ import annotations

import datetime as dt
import os
import random

import duckdb
import pytest

from studio_pulse import alerts
from studio_pulse.stats import forecast

START = dt.date(2026, 1, 5)  # Monday
HOLIDAY = START + dt.timedelta(days=70)
STEP = START + dt.timedelta(days=100)


def series(seed: int = 3) -> list[tuple[dt.date, float]]:
    rng = random.Random(seed)
    out = []
    for i in range(120):
        d = START + dt.timedelta(days=i)
        v = 1000 * (1 + 0.002 * i) * (0.8 if d.weekday() >= 5 else 1.0) * (1 + rng.gauss(0, 0.03))
        if d == HOLIDAY:
            v *= 0.75
        if d >= STEP:
            v *= 0.7
        out.append((d, round(v)))
    return out


def trailing_mean_alerts(s: list[tuple[dt.date, float]]) -> list[dt.date]:
    """The existing drop_vs_baseline rule (14-day mean, -15%), evaluated day by day."""
    rule = alerts.Rule.from_dict(
        {
            "name": "dau_drop",
            "metric": "dau",
            "condition": "drop_vs_baseline",
            "baseline_days": 14,
            "threshold_pct": 15,
        },
        {},
    )
    con = duckdb.connect()
    con.execute("CREATE TABLE full_series (app_id VARCHAR, date DATE, dau DOUBLE)")
    con.executemany("INSERT INTO full_series VALUES ('a', ?, ?)", [[d, v] for d, v in s])
    fired = []
    for d, _ in s:
        con.execute(
            "CREATE OR REPLACE VIEW fct_app_daily AS SELECT * FROM full_series "
            f"WHERE date <= DATE '{d.isoformat()}'"
        )
        fired += [a.date for a in alerts.evaluate(con, [rule])]
    con.close()
    return fired


def check(engine: str, seed: int) -> tuple[int, int]:
    """(false alerts before the step, trailing-mean alerts) for one series."""
    s = series(seed)
    fired = forecast.backtest(s, engine=engine, holidays={HOLIDAY}, direction="both")
    assert STEP in fired, f"{engine} seed {seed}: the -30% step was not caught on its first day"
    assert HOLIDAY not in fired
    assert not [d for d in fired if HOLIDAY < d <= HOLIDAY + dt.timedelta(days=7)]
    trailing = [d for d in trailing_mean_alerts(s) if d < STEP and d != HOLIDAY]
    assert all(d.weekday() >= 5 for d in trailing[:5])  # weekend dips
    return len([d for d in fired if d < STEP]), len(trailing)


def test_seasonal_engine_backtest():
    false, trailing = check("seasonal", 3)
    assert false <= 1 and trailing >= 5, (false, trailing)


def need_statsforecast() -> None:
    """Skip without the extra, except in the CI job that installs it (there it must run)."""
    if os.environ.get("PULSE_REQUIRE_FORECAST") == "1":
        import statsforecast  # noqa: F401
    else:
        pytest.importorskip("statsforecast")


@pytest.mark.forecast
def test_statsforecast_engine_backtest():
    need_statsforecast()
    results = [check("statsforecast", seed) for seed in (1, 2, 3, 4)]
    false = [f for f, _ in results]
    # AutoETS at its default 99.9% level: at most one false alert per 120-day series on average
    assert sum(false) <= len(results), false
    assert all(t >= 5 for _, t in results)


def test_a_holiday_not_listed_would_alert():
    s = series()
    fired = forecast.backtest(s, engine="seasonal", holidays=set())
    assert HOLIDAY in fired


def test_seasonal_band_by_hand():
    """Flat weeks of [100, 100, 100, 100, 100, 80, 80] with no noise: the band is the
    weekday value +- the 2% floor."""
    week = [100, 100, 100, 100, 100, 80, 80]
    hist = [(START + dt.timedelta(days=i), float(week[i % 7])) for i in range(42)]
    sat = START + dt.timedelta(days=47)  # a Saturday
    b = forecast.seasonal_band(hist, sat, level=99)
    assert b.center == pytest.approx(80.0) and b.engine == "seasonal"
    assert (b.low, b.high) == pytest.approx((80 * 0.98, 80 * 1.02))
    mon = START + dt.timedelta(days=42)
    assert forecast.seasonal_band(hist, mon).center == pytest.approx(100.0)
    assert forecast.seasonal_band(hist[:20], mon) is None  # too little history


def test_rule_through_the_alert_engine():
    s = series()
    con = duckdb.connect()
    con.execute("CREATE TABLE fct_app_daily (app_id VARCHAR, date DATE, dau DOUBLE)")
    con.executemany("INSERT INTO fct_app_daily VALUES ('a', ?, ?)", [[d, v] for d, v in s[:101]])
    rule = alerts.Rule.from_dict(
        {
            "name": "dau_band",
            "metric": "dau",
            "condition": "forecast_band",
            "level": 99.5,
            "direction": "drop",
            "engine": "seasonal",
            "holidays": [HOLIDAY.isoformat()],
        },
        {},
    )
    fired = alerts.evaluate(con, [rule])
    assert [a.date for a in fired] == [STEP]
    assert "forecast band" in fired[0].message and fired[0].baseline > fired[0].value
    with pytest.raises(alerts.RuleError, match="direction"):
        alerts.Rule.from_dict(
            {"name": "x", "metric": "dau", "condition": "forecast_band", "direction": "sideways"},
            {},
        )
    with pytest.raises(alerts.RuleError, match="evaluate_days"):
        alerts.Rule.from_dict(
            {"name": "x", "metric": "dau", "condition": "forecast_band", "evaluate_days": 3}, {}
        )
    # holiday: no alert on the day itself even when it is the latest point
    con.execute("DELETE FROM fct_app_daily WHERE date > ?", [HOLIDAY])
    assert alerts.evaluate(con, [rule]) == []
    con.close()


def test_auto_engine_without_the_extra(monkeypatch):
    monkeypatch.setattr(forecast, "statsforecast_available", lambda: False)
    s = series()
    b = forecast.band(s[:60], s[60][0], engine="auto")
    assert b.engine == "seasonal"
    with pytest.raises(ValueError, match="forecast"):
        forecast.band(s[:60], s[60][0], engine="statsforecast")
