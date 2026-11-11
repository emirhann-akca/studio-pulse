"""Experiment statistics: checked against scipy / statsmodels, a published example
and GrowthBook's formulas, plus the extraction query on a GA4-shaped file."""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest
from conftest import D0, GA4Builder

from studio_pulse.stats import dist
from studio_pulse.stats import experiments as X

scipy_stats = pytest.importorskip("scipy.stats")


# -- distributions -------------------------------------------------------------------


@pytest.mark.parametrize("df", [1, 2.5, 7, 30, 15.497, 400, 25_000, 2_000_000])
@pytest.mark.parametrize("t", [0.0, 0.3, 1.0, 1.96, 2.2694, 4.0, -2.5])
def test_t_distribution_matches_scipy(t, df):
    assert dist.t_cdf(t, df) == pytest.approx(scipy_stats.t.cdf(t, df), abs=1e-12)
    assert dist.t_sf_two_sided(t, df) == pytest.approx(
        2 * scipy_stats.t.sf(abs(t), df), rel=1e-9, abs=1e-15
    )


@pytest.mark.parametrize("df", [1, 3, 15.497, 120, 1e6])
@pytest.mark.parametrize("p", [0.6, 0.9, 0.95, 0.975, 0.995])
def test_t_quantile_matches_scipy(p, df):
    assert dist.t_ppf(p, df) == pytest.approx(scipy_stats.t.ppf(p, df), abs=1e-9)


@pytest.mark.parametrize("df", [1, 2, 3, 9])
@pytest.mark.parametrize("x", [0.01, 0.5, 3.84, 16.0, 22.77, 80.0])
def test_chi2_matches_scipy(x, df):
    assert dist.chi2_sf(x, df) == pytest.approx(scipy_stats.chi2.sf(x, df), rel=1e-9)


# -- SRM ---------------------------------------------------------------------------------


def test_srm_52_48_at_10000_is_flagged():
    s = X.srm([5200, 4800], [0.5, 0.5])
    # chi2 = 2 x 200^2 / 5000 = 16, df 1
    assert s["chi2"] == pytest.approx(16.0) and s["df"] == 1
    assert s["p_value"] == pytest.approx(scipy_stats.chisquare([5200, 4800]).pvalue, rel=1e-9)
    assert s["p_value"] < X.SRM_THRESHOLD
    ok = X.srm([5040, 4960], [0.5, 0.5])
    assert ok["p_value"] > 0.4


def test_srm_uneven_split_and_three_arms():
    got = X.srm([3300, 3300, 3400], [1, 1, 1])
    want = scipy_stats.chisquare([3300, 3300, 3400], [10000 / 3] * 3)
    assert got["chi2"] == pytest.approx(want.statistic) and got["df"] == 2
    assert got["p_value"] == pytest.approx(want.pvalue, rel=1e-9)
    assert X.srm([9000, 1000], [0.9, 0.1])["chi2"] == pytest.approx(0.0)


# -- two proportions -----------------------------------------------------------------


def test_two_proportions_match_statsmodels():
    sm = pytest.importorskip("statsmodels.stats.proportion")
    r = X.two_proportions(120, 2400, 156, 2380, alpha=0.05)
    z, p = sm.proportions_ztest([156, 120], [2380, 2400])  # pooled, as here
    assert r["z"] == pytest.approx(z, rel=1e-12) and r["p_value"] == pytest.approx(p, rel=1e-9)
    lo, hi = sm.confint_proportions_2indep(156, 2380, 120, 2400, method="wald", compare="diff")
    assert (r["ci_low"], r["ci_high"]) == pytest.approx((lo, hi), abs=1e-12)
    assert r["estimate"] == pytest.approx(156 / 2380 - 120 / 2400)


# -- Welch -----------------------------------------------------------------------------

NIST_OLD = [32, 37, 35, 28, 41, 44, 35, 31, 34, 38, 42]
NIST_NEW = [36, 31, 30, 31, 34, 36, 29, 32, 31]


def _arm(name: str, ys: list[float], xs: list[float] | None = None) -> X.Arm:
    a = X.Arm(name, len(ys), y_sum=sum(ys), y_sumsq=sum(y * y for y in ys))
    if xs is not None:
        a.x_sum, a.x_sumsq = sum(xs), sum(x * x for x in xs)
        a.xy_sum = sum(x * y for x, y in zip(xs, ys, strict=True))
    return a


def test_welch_reproduces_the_nist_handbook_example():
    """NIST/SEMATECH e-Handbook of Statistical Methods, 7.3.1 "Do two processes
    have the same mean?" (https://www.itl.nist.gov/div898/handbook/prc/section3/prc31.htm):
    old process n=11, mean 36.0909, sd 4.9082; new n=9, mean 32.2222, sd 2.5386;
    unequal-variance t = 2.2694 with about 15.5 degrees of freedom."""
    old, new = _arm("old", NIST_OLD), _arm("new", NIST_NEW)
    assert old.mean_y == pytest.approx(36.0909, abs=5e-5)
    assert math.sqrt(old.var_y) == pytest.approx(4.9082, abs=5e-5)
    assert math.sqrt(new.var_y) == pytest.approx(2.5386, abs=5e-5)
    r = X.welch(new.mean_y, new.var_y, new.users, old.mean_y, old.var_y, old.users)
    assert r["t"] == pytest.approx(2.2694, abs=5e-5)
    assert round(r["df"], 1) == 15.5
    # the handbook's one-sided test rejects at 0.05 (critical value 1.746 at 16 df)
    assert r["p_value"] / 2 < 0.05


def test_welch_matches_scipy_from_stats_and_raw_data():
    rng = random.Random(7)
    a = [rng.gauss(10, 3) for _ in range(800)]
    b = [rng.gauss(10.4, 4) for _ in range(650)]
    A, B = _arm("A", a), _arm("B", b)
    r = X.welch(A.mean_y, A.var_y, A.users, B.mean_y, B.var_y, B.users, alpha=0.05)
    ref = scipy_stats.ttest_ind(b, a, equal_var=False)
    assert r["t"] == pytest.approx(ref.statistic, rel=1e-9)
    assert r["p_value"] == pytest.approx(ref.pvalue, rel=1e-8)
    ci = ref.confidence_interval(0.95)
    assert (r["ci_low"], r["ci_high"]) == pytest.approx((ci.low, ci.high), rel=1e-9)
    assert r["df"] == pytest.approx(ref.df, rel=1e-10)


# -- CUPED -------------------------------------------------------------------------------


def _correlated(n: int, rho: float, shift: float, rng: random.Random):
    xs, ys = [], []
    for _ in range(n):
        x = rng.gauss(0, 1)
        y = rho * x + math.sqrt(1 - rho * rho) * rng.gauss(0, 1) + shift
        xs.append(x + 5)
        ys.append(y + 5)
    return xs, ys


def test_cuped_equals_regression_on_raw_data_and_reduces_variance_by_rho_squared():
    rng = random.Random(42)
    rho = 0.7
    xa, ya = _correlated(60_000, rho, 0.0, rng)
    xb, yb = _correlated(60_000, rho, 0.02, rng)
    A, B = _arm("A", ya, xa), _arm("B", yb, xb)
    res = X.analyze(X.Experiment("t", [A, B], control="A", split={"A": 1, "B": 1}))
    cu = res["comparisons"][0]["cuped"]
    # Independent computation on the raw rows: theta from all users, then Welch
    allx, ally = xa + xb, ya + yb
    mx, my = sum(allx) / len(allx), sum(ally) / len(ally)
    theta = sum((x - mx) * (y - my) for x, y in zip(allx, ally, strict=True)) / sum(
        (x - mx) ** 2 for x in allx
    )
    assert cu["theta"] == pytest.approx(theta, rel=1e-9)
    adj_a = [y - theta * x for x, y in zip(xa, ya, strict=True)]
    adj_b = [y - theta * x for x, y in zip(xb, yb, strict=True)]
    ref = scipy_stats.ttest_ind(adj_b, adj_a, equal_var=False)
    assert cu["t"] == pytest.approx(ref.statistic, rel=1e-7)
    assert cu["p_value"] == pytest.approx(ref.pvalue, rel=1e-6)
    # variance reduction = 1 - rho^2 = 0.51, within 2% (relative)
    assert cu["variance_reduction"] == pytest.approx(rho * rho, rel=0.02)
    # statsmodels OLS y ~ treatment + x: the same effect (pooled theta ~ OLS slope)
    sm = pytest.importorskip("statsmodels.api")
    import numpy as np

    y = np.array(ya + yb)
    design = sm.add_constant(np.column_stack([[0] * len(ya) + [1] * len(yb), allx]))
    ols = sm.OLS(y, design).fit()
    assert cu["estimate"] == pytest.approx(ols.params[1], abs=2e-4)
    assert cu["se"] == pytest.approx(ols.bse[1], rel=0.02)


def test_cuped_without_signal_in_the_covariate_changes_nothing():
    a = X.Arm("A", 1000, y_sum=500, y_sumsq=700, x_sum=0, x_sumsq=0, xy_sum=0)
    b = X.Arm("B", 1000, y_sum=520, y_sumsq=730, x_sum=0, x_sumsq=0, xy_sum=0)
    assert X.cuped_theta([a, b]) == 0.0


# -- sequential (GrowthBook port) --------------------------------------------------------


def test_sequential_matches_growthbook_formulas():
    """Waudby-Smith et al. eq. 9 / 155 as written in gbstats (computed here with numpy)."""
    np = pytest.importorskip("numpy")
    alpha, tuning, n, est, se = 0.05, 5000.0, 20_000, 0.031, 0.012
    rho = np.sqrt((-2 * np.log(alpha) + np.log(-2 * np.log(alpha) + 1)) / tuning)
    s2 = se**2 * n
    half = np.sqrt(s2) * np.sqrt(
        (2 * (n * rho**2 + 1)) * np.log(np.sqrt(n * rho**2 + 1) / alpha) / (n * rho) ** 2
    )
    st2 = est**2 * n / se**2
    tr2p1 = n * rho**2 + 1
    p = min(1 / (np.exp(rho**2 * st2 / (2 * tr2p1)) / np.sqrt(tr2p1)), 1)
    r = X.sequential(est, se, n, alpha, tuning)
    assert r["rho"] == pytest.approx(float(rho), rel=1e-12)
    assert r["halfwidth"] == pytest.approx(float(half), rel=1e-12)
    assert r["p_value"] == pytest.approx(float(p), rel=1e-10)
    # always wider than the fixed-horizon interval
    assert r["halfwidth"] > dist.norm_ppf(0.975) * se


def test_sequential_keeps_false_positives_under_alpha_when_peeking():
    """A/A tests looked at 20 times: fixed-horizon tests 'find' effects far more often."""
    rng = random.Random(2026)
    sims, looks, per_look = 300, 20, 100
    fixed_fp = seq_fp = 0
    for _ in range(sims):
        sa = sb = ssa = ssb = 0.0
        na = nb = 0
        fixed_hit = seq_hit = False
        for _ in range(looks):
            for _ in range(per_look):
                ya, yb = rng.gauss(0, 1), rng.gauss(0, 1)
                sa, ssa, sb, ssb = sa + ya, ssa + ya * ya, sb + yb, ssb + yb * yb
            na += per_look
            nb += per_look
            A = X.Arm("A", na, y_sum=sa, y_sumsq=ssa)
            B = X.Arm("B", nb, y_sum=sb, y_sumsq=ssb)
            w = X.welch(A.mean_y, A.var_y, na, B.mean_y, B.var_y, nb)
            fixed_hit |= w["p_value"] < 0.05
            seq_hit |= X.sequential(w["estimate"], w["se"], na + nb)["p_value"] < 0.05
        fixed_fp += fixed_hit
        seq_fp += seq_hit
    assert fixed_fp / sims > 0.15  # peeking inflates the 5% error rate several-fold
    assert seq_fp / sims <= 0.05


# -- the whole analysis -------------------------------------------------------------------


def test_analyze_reports_every_test_and_flags_srm():
    a = X.Arm("A", 5200, conversions=260, y_sum=5200 * 1.0, y_sumsq=5200 * 3.0)
    b = X.Arm("B", 4800, conversions=264, y_sum=4800 * 1.1, y_sumsq=4800 * 3.3)
    res = X.analyze(X.Experiment("srm_case", [b, a], control="A", split={"A": 0.5, "B": 0.5}))
    assert res["control"] == "A" and res["srm"]["flagged"]
    assert "Sample ratio mismatch" in res["verdict"]
    c = res["comparisons"][0]
    assert set(c) == {"variant", "conversion", "mean", "sequential"}
    assert "SAMPLE RATIO MISMATCH" in X.format_console(res)


def test_read_arms_csv_and_cli(tmp_path, capsys):
    from studio_pulse import cli

    f = tmp_path / "paywall.csv"
    f.write_text(
        "variant,users,conversions,y_sum,y_sumsq,x_sum,x_sumsq,xy_sum\n"
        "A,5000,400,6000,30000,5800,29000,25000\n"
        "B,5050,455,6600,33500,5900,29600,26100\n",
        encoding="utf-8",
    )
    arms = X.read_arms(f)
    assert [a.variant for a in arms] == ["A", "B"] and arms[1].conversions == 455
    rc = cli.main(["experiment", "--input", str(f), "--control", "A", "--split", "A=1,B=1"])
    out = capsys.readouterr().out
    assert rc == 0 and "mean (CUPED)" in out and "always-valid" in out
    rc = cli.main(["experiment", "--input", str(f), "--json"])
    assert rc == 0 and '"comparisons"' in capsys.readouterr().out


# -- extraction: per-variant sums inside the source, no user ids ---------------------------


def test_experiment_query_on_a_ga4_file(con, tmp_path):
    from studio_pulse.connectors import ga4
    from studio_pulse.connectors.dialect import DuckDBDialect
    from studio_pulse.connectors.executors import DuckDBExecutor

    b = GA4Builder.__new__(GA4Builder)
    GA4Builder.__init__(b)
    b.PARAMS = {**GA4Builder.PARAMS, "experiment_id": "string", "variant": "string"}

    def ev(day, name, user, **kw):
        b.add(day, name, user, **kw)

    # u1 (A): 2 events before exposure, exposed on day 14, 3 events in the window
    ev(12, "booster_used", "u1")
    ev(13, "booster_used", "u1")
    ev(14, "experiment_exposure", "u1", experiment_id="exp1", variant="A")
    for d in (14, 15, 20):
        ev(d, "booster_used", "u1")
    ev(21, "booster_used", "u1")  # day 7 after exposure: outside the 7-day window
    # u2 (B): exposed day 15, 1 event in the window, a purchase (conversion)
    ev(15, "experiment_exposure", "u2", experiment_id="exp1", variant="B")
    ev(16, "booster_used", "u2")
    ev(16, "in_app_purchase", "u2", value_usd=1.99)
    # u3 saw both variants: dropped. u4 exposed to another experiment: ignored.
    ev(14, "experiment_exposure", "u3", experiment_id="exp1", variant="A")
    ev(15, "experiment_exposure", "u3", experiment_id="exp1", variant="B")
    ev(14, "experiment_exposure", "u4", experiment_id="other", variant="B")
    # u5 (B) exposed on day 25: its window ends after the data -> not counted yet
    ev(25, "experiment_exposure", "u5", experiment_id="exp1", variant="B")
    path = b.write(tmp_path / "e.parquet")
    spec = ga4.ExperimentSpec.from_dict(
        {
            "id": "exp1",
            "start": D0 + dt.timedelta(days=14),
            "end": D0 + dt.timedelta(days=26),
            "metric_event": "booster_used",
            "conversion_event": "in_app_purchase",
            "window_days": 7,
            "pre_days": 14,
        }
    )
    q = ga4.GA4Queries(DuckDBDialect(), D0, D0 + dt.timedelta(days=27), ga4.GA4Options())
    t = DuckDBExecutor({"ga4_events": path}).run("exp", q.experiment_arms(spec))
    assert not {"user_pseudo_id", "u", "user_id"} & set(t.column_names)
    got = {r["variant"]: r for r in t.to_pylist()}
    assert set(got) == {"A", "B"}
    a, bb = got["A"], got["B"]
    assert (a["users"], a["conversions"], a["y_sum"], a["y_sumsq"]) == (1, 0, 3, 9)
    assert (a["x_sum"], a["x_sumsq"], a["xy_sum"]) == (2, 4, 6)
    assert (bb["users"], bb["conversions"], bb["y_sum"], bb["x_sum"]) == (1, 1, 1, 0)
    for col in t.columns:
        assert not any(str(v).startswith("u") and str(v)[1:].isdigit() for v in col.to_pylist())
    with pytest.raises(ValueError):
        ga4.ExperimentSpec.from_dict(
            {"id": "x'; DROP", "start": D0, "end": D0, "metric_event": "e"}
        )
