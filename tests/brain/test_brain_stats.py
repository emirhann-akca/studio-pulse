"""The brain's statistics against scipy / statsmodels and brute-force references."""

from __future__ import annotations

import math
import random

import pytest

from studio_pulse.brain import stats


def test_wilson_matches_statsmodels():
    from statsmodels.stats.proportion import proportion_confint

    for x, n in ((0, 10), (3, 10), (23856, 39048), (999, 1000), (1, 2)):
        lo, hi = stats.wilson(x, n)
        rlo, rhi = proportion_confint(x, n, alpha=0.05, method="wilson")
        assert lo == pytest.approx(rlo, abs=1e-9) and hi == pytest.approx(rhi, abs=1e-9)


def test_quartz_level_7_example_from_the_research():
    lo, hi = stats.wilson(round(0.6091 * 39048), 39048)
    assert lo == pytest.approx(0.6042, abs=5e-4) and hi == pytest.approx(0.6139, abs=5e-4)


def test_two_proportions_matches_statsmodels():
    from statsmodels.stats.proportion import proportions_ztest

    t = stats.two_proportions(97, 364, 62, 385)
    z, p = proportions_ztest([62, 97], [385, 364])
    assert t.z == pytest.approx(z, rel=1e-9) and t.p_value == pytest.approx(p, rel=1e-9)
    assert t.diff == pytest.approx(62 / 385 - 97 / 364)
    assert stats.two_proportions(1, 0, 1, 10) is None


def test_benjamini_hochberg_matches_statsmodels():
    from statsmodels.stats.multitest import multipletests

    rng = random.Random(4)
    ps = [rng.random() ** 3 for _ in range(40)]
    ref = multipletests(ps, alpha=0.1, method="fdr_bh")[1]
    got = stats.benjamini_hochberg(ps)
    assert all(g == pytest.approx(r, rel=1e-12) for g, r in zip(got, ref, strict=True))
    assert stats.benjamini_hochberg([None, 0.01, None]) == [None, 0.01, None]


def test_mann_whitney_matches_scipy():
    from scipy.stats import mannwhitneyu

    rng = random.Random(7)
    a = [round(rng.gauss(18.4, 0.9), 1) for _ in range(300)]  # rounding makes ties
    b = [round(rng.gauss(18.7, 0.9), 1) for _ in range(250)]
    u, p = stats.mann_whitney(a, b)
    ref = mannwhitneyu(a, b, alternative="two-sided", method="asymptotic", use_continuity=False)
    assert u == pytest.approx(ref.statistic) and p == pytest.approx(ref.pvalue, rel=1e-9)


def test_median_and_mad():
    assert stats.median([3, 1, 2]) == 2 and stats.median([4, 1, 2, 3]) == 2.5
    xs = [0.2, 0.21, 0.19, 0.6, 0.2]
    assert stats.mad(xs) == pytest.approx(1.4826 * 0.01)
    lo, hi = stats.median_ci(list(range(100)))
    assert lo < 49.5 < hi


def test_js_divergence_matches_scipy():
    """Adtributor's per-element surprise sums to the Jensen-Shannon divergence of the shares."""
    from scipy.spatial.distance import jensenshannon

    P = [0.5, 0.3, 0.2, 0.0]
    Q = [0.2, 0.3, 0.25, 0.25]
    got = sum(stats.js_divergence(p, q) for p, q in zip(P, Q, strict=True))
    assert got == pytest.approx(jensenshannon(P, Q, base=2) ** 2, rel=1e-9)
    assert stats.js_divergence(0.4, 0.4) == 0


def _km_bruteforce(rows):
    """Kaplan-Meier from individual records: (time, event) per user."""
    recs = []
    for step, _n, d, c in rows:
        recs += [(step, 1)] * int(d) + [(step, 0)] * int(c)
    s = 1.0
    out = {}
    for t in sorted({r[0] for r in recs}):
        at_risk = sum(1 for r in recs if r[0] >= t)
        deaths = sum(1 for r in recs if r[0] == t and r[1])
        s *= 1 - deaths / at_risk
        out[t] = s
    return out


def test_kaplan_meier_from_aggregates_equals_individual_records():
    # at level k: at_risk = users reaching k, events = churned at k, censored = stopped at k, active
    stopped = {1: (30, 10), 2: (25, 5), 3: (40, 20), 4: (10, 10), 5: (5, 25)}
    rows = []
    reach = sum(d + c for d, c in stopped.values())
    for k in sorted(stopped):
        d, c = stopped[k]
        rows.append((k, reach, d, c))
        reach -= d + c
    km = stats.kaplan_meier(rows)
    ref = _km_bruteforce(rows)
    for p in km:
        assert p.survival == pytest.approx(ref[p.step], rel=1e-12)
        assert p.ci[0] <= p.survival <= p.ci[1]


def test_kaplan_meier_greenwood_matches_textbook():
    """Greenwood variance: Var S = S^2 sum d / (n (n - d)), log(-log) interval."""
    km = stats.kaplan_meier([(1, 100, 10, 0), (2, 90, 9, 0)])
    s = 0.9 * 0.9
    var = s**2 * (10 / (100 * 90) + 9 / (90 * 81))
    se = math.sqrt(var / s**2) / abs(math.log(s))
    assert km[-1].survival == pytest.approx(s)
    assert km[-1].ci[0] == pytest.approx(s ** math.exp(1.959963984540054 * se))


def test_logrank_matches_scipy_when_available():
    scipy_stats = pytest.importorskip("scipy.stats")
    if not hasattr(scipy_stats, "logrank"):
        pytest.skip("scipy without logrank")
    from scipy.stats import CensoredData, logrank

    a_rows = [(1, 50, 5), (2, 45, 5), (3, 40, 8)]
    b_rows = [(1, 50, 2), (2, 48, 3), (3, 45, 2)]
    chi2, p = stats.logrank(a_rows, b_rows)

    def expand(rows, total):
        times, cens = [], []
        alive = total
        for t, _n, d in rows:
            times += [t] * d
            alive -= d
        cens = [4] * alive
        return CensoredData(uncensored=times, right=cens)

    ref = logrank(expand(a_rows, 50), expand(b_rows, 50))
    assert p == pytest.approx(ref.pvalue, rel=1e-6)
    assert chi2 == pytest.approx(ref.statistic**2, rel=1e-6)


def test_welch_mean_diff_matches_scipy():
    from scipy.stats import ttest_ind

    a = [1.0, 2.0, 2.5, 3.0, 2.2, 1.9]
    b = [2.5, 3.1, 3.3, 2.9, 3.8]
    diff, ci, p = stats.welch_mean_diff(a, b)
    ref = ttest_ind(b, a, equal_var=False)
    assert p == pytest.approx(ref.pvalue, rel=1e-6)
    assert ci[0] < diff < ci[1]
