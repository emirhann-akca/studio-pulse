"""Predicted LTV v1: exact fits on noise-free curves, a backtest on synthetic
cohorts with a known truth, and no look-ahead in the calibration."""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest

from studio_pulse.stats import pltv

START = dt.date(2026, 6, 1)  # a Monday


def cohorts(curve, days: int = 70, last_day: int | None = None, size: int = 200, noise=None):
    """(cohort_date, day_n, size, cum_revenue) for `days` cohorts, complete cells only."""
    last = START + dt.timedelta(days=(last_day if last_day is not None else days + 40))
    rows = []
    for c in range(days):
        cd = START + dt.timedelta(days=c)
        k = noise(c) if noise else 1.0
        for n in range(0, 91):
            if cd + dt.timedelta(days=n) > last:
                break
            rows.append((cd, n, size, size * curve(n) * k))
    return rows


def test_power_fit_is_exact_on_a_power_curve():
    f = pltv.fit("power", {t: 0.05 * (t + 1) ** 0.5 for t in range(8)})
    assert f.a == pytest.approx(0.05) and f.b == pytest.approx(0.5)
    # D30 = 0.05 x 31^0.5 = 0.27838821814150...
    assert f.predict(30) == pytest.approx(0.2783882181415011, rel=1e-12)


def test_log_fit_is_exact_on_a_log_curve():
    f = pltv.fit("log", {t: 0.1 + 0.05 * math.log(t + 1) for t in range(8)})
    # D30 = 0.1 + 0.05 ln 31 = 0.27169936...
    assert f.predict(30) == pytest.approx(0.1 + 0.05 * math.log(31), rel=1e-12)
    assert pltv.fit("log", {0: 0.1, 1: 0.2}) is None  # fewer than 3 points


def test_noise_free_cohorts_backtest_to_zero_error():
    rows = cohorts(lambda n: 0.05 * (n + 1) ** 0.5)
    res = pltv.predict_app(rows)
    assert res["model"] == "power"
    assert res["mape"]["power"] == pytest.approx(0.0, abs=1e-12)
    assert res["factor"] == pytest.approx(1.0)
    assert pltv.predicted(res, 30) == pytest.approx(0.2783882181415011, rel=1e-9)


def test_backtest_on_noisy_synthetic_cohorts_with_known_truth():
    """Weekly groups get a random level (+-10%), each cohort its own noise (+-5%); the
    curve shape is a power law with b = 0.45. Fit on D0-D7, predict D30."""
    rng = random.Random(11)
    week_level = {w: rng.lognormvariate(0, 0.1) for w in range(12)}
    noise = {c: week_level[c // 7] * rng.lognormvariate(0, 0.05) for c in range(70)}
    truth = lambda n: 0.04 * (n + 1) ** 0.45  # noqa: E731
    rows = cohorts(truth, noise=lambda c: noise[c])
    res = pltv.predict_app(rows)
    tests = res["tests"]["power"]
    assert len(tests) >= 5
    # the shape is right, so the error is the group noise at D30 relative to D7: ~0
    assert pltv.mape(tests) < 0.02
    assert res["model"] == "power"
    # the recent pool's prediction brackets its own (known) D30
    pool = {cd for cd, n, _, _ in rows if n == 7}
    recent = sorted(pool)[-pltv.RECENT_COHORTS :]
    true_d30 = sum(truth(30) * noise[(cd - START).days] for cd in recent) / len(recent)
    assert pltv.predicted(res, 30, "lo_ratio") <= true_d30 * 1.02
    assert pltv.predicted(res, 30, "hi_ratio") >= true_d30 * 0.98


def test_a_wrong_shape_is_corrected_by_calibration_without_look_ahead():
    """Truth flattens faster than a power law: raw extrapolation overshoots by a
    stable factor, which earlier groups teach the calibration."""
    truth = lambda n: 0.1 * (1 - math.exp(-n / 6)) + 0.02  # noqa: E731
    rows = cohorts(truth, days=84, last_day=124)
    groups = pltv.groups_from_rows(rows)
    bt = pltv.backtest(groups, "power")
    first, last = bt[0], bt[-1]
    assert first["train_groups"] == 0 and first["calibration"] == 1.0  # nothing known yet
    assert last["train_groups"] >= 1 and last["ape"] < first["ape"] / 5
    # a group is never calibrated with its own or a later group's D30
    for r in bt:
        g = next(x for x in groups if x.key == r["cohort_group"])
        known_by = g.last_cohort + dt.timedelta(days=7)
        assert r["train_groups"] == sum(
            1
            for h in groups
            if h.cohorts >= 7
            and 30 in h.curve
            and h.last_cohort + dt.timedelta(days=30) <= known_by
        )


def test_prediction_never_below_observed_day7(con):
    # A step curve (trial converts on day 7): no smooth shape fits; the floor holds.
    rows = cohorts(lambda n: 0.0 if n < 7 else 0.5 + 0.004 * (n - 7))
    res = pltv.predict_app(rows)
    for t in (14, 30, 90):
        assert pltv.predicted(res, t) >= res["floor"] == pytest.approx(0.5)


def test_store_writes_the_three_tables(con):
    con.execute(
        "CREATE TABLE fct_cohort_ltv (app_id VARCHAR, cohort_date DATE, day_n INTEGER, "
        "cohort_size BIGINT, cum_iap_usd DOUBLE, cum_ad_usd DOUBLE, cum_revenue_usd DOUBLE, "
        "cum_payers BIGINT)"
    )
    for cd, n, size, rev in cohorts(lambda n: 0.05 * (n + 1) ** 0.5):
        con.execute(
            "INSERT INTO fct_cohort_ltv VALUES ('g', ?, ?, ?, 0, ?, ?, 0)", [cd, n, size, rev, rev]
        )
    assert pltv.store(con) == 1
    s = dict(
        con.execute(
            "SELECT horizon_day, predicted_arpu_usd FROM fct_pltv_summary WHERE app_id = 'g'"
        ).fetchall()
    )
    assert set(s) == set(pltv.HORIZONS)
    assert s[30] == pytest.approx(0.278388218, rel=1e-8)  # stored with 9 significant digits
    curve = con.execute(
        "SELECT day_n, observed_arpu_usd, predicted_arpu_usd FROM fct_pltv_curve ORDER BY day_n"
    ).fetchall()
    assert len(curve) == pltv.CURVE_DAYS + 1
    assert curve[3][1] == curve[3][2]  # observed days are shown as observed
    assert con.execute("SELECT COUNT(*) FROM fct_pltv_backtest").fetchone()[0] > 0
