"""B4: Adtributor + DIFF-lite on synthetic cohort cells, and on the demo's D1 drop."""

from __future__ import annotations

import itertools
import random

import pytest

from studio_pulse.brain import attribution as A
from studio_pulse.brain import stats

COUNTRIES = {"US": 0.35, "BR": 0.2, "IN": 0.2, "DE": 0.15, "TR": 0.1}
VERSIONS = {"1.7.2": 0.5, "1.8.0": 0.5}
TIERS = {"low": 0.3, "mid": 0.45, "high": 0.25}


def make_cells(rate_fn, users: int, seed: int):
    """Binomial cohorts per (country, version, tier) for a baseline (0) and a current (1) period."""
    rng = random.Random(seed)
    base: dict[tuple[str, str, str], list[float]] = {}
    for (c, wc), (v, wv), (t, wt) in itertools.product(
        COUNTRIES.items(), VERSIONS.items(), TIERS.items()
    ):
        out = []
        for period in (0, 1):
            n = round(users * wc * wv * wt)
            r = rate_fn(period, c, v, t)
            x = sum(rng.random() < r for _ in range(n))
            out += [n, x]
        base[(c, v, t)] = out
    totals = A.Totals(*[sum(v[i] for v in base.values()) for i in range(4)])
    dims = {"country": 0, "app_version": 1, "device_tier": 2}

    def elements(keys: tuple[str, ...]) -> list[A.Element]:
        acc: dict[str, list[float]] = {}
        for cell, v in base.items():
            label = " | ".join(cell[dims[k]] for k in keys)
            a = acc.setdefault(label, [0.0] * 4)
            for i in range(4):
                a[i] += v[i]
        return [A.Element(k, *v) for k, v in sorted(acc.items())]

    singles = {k: elements((k,)) for k in dims}
    pairs = {f"{a},{b}": elements((a, b)) for a, b in itertools.combinations(dims, 2)}
    return totals, singles, pairs


def explain(totals, singles, pairs):
    decomps = [A.decompose(d, A.with_other(e, totals), totals) for d, e in singles.items()]
    cands = A.adtributor(decomps)
    cells = A.diff_cells({**singles, **pairs}, totals)
    for c, q in zip(cells, stats.benjamini_hochberg([c.p for c in cells]), strict=True):
        c.q = q
    conf = A.confirmed(cells, 0.10)
    return decomps, cands, conf, A.verdict(cands, conf)


def test_decomposition_has_no_residual():
    totals, singles, _ = make_cells(lambda p, c, v, t: 0.3 - 0.15 * p * (c == "BR"), 40000, 1)
    for d, els in singles.items():
        dec = A.decompose(d, A.with_other(els, totals), totals)
        assert dec.mix + dec.rate == pytest.approx(totals.delta, abs=1e-12)
        assert sum(r.contribution for r in dec.rows) == pytest.approx(totals.delta, abs=1e-12)


def test_single_dimension_cause_country_br_is_found_first():
    totals, singles, pairs = make_cells(
        lambda p, c, v, t: 0.30 - (0.15 if p and c == "BR" else 0.0), 40000, 2
    )
    _, cands, conf, (kind, cause, cand) = explain(totals, singles, pairs)
    assert cands[0].dimension == "country" and cands[0].values == ["BR"]
    assert kind == "single" and cause.dimension == "country" and cause.value == "BR"
    assert cause.rate < cause.rest_rate


def test_two_dimension_cause_version_by_tier_is_found_first():
    totals, singles, pairs = make_cells(
        lambda p, c, v, t: 0.30 - (0.20 if p and v == "1.8.0" and t == "low" else 0.0), 40000, 3
    )
    _, _, conf, (kind, cause, _) = explain(totals, singles, pairs)
    assert kind == "pair"
    assert cause.dimension == "app_version,device_tier" and cause.value == "1.8.0 | low"
    assert set(cause.parents) == {"app_version=1.8.0", "device_tier=low"}


@pytest.mark.parametrize("seed", [11, 12, 13, 14, 15])
def test_noise_only_returns_nothing(seed):
    totals, singles, pairs = make_cells(lambda p, c, v, t: 0.30, 40000, seed)
    _, _, conf, (kind, cause, _) = explain(totals, singles, pairs)
    assert conf == [] and kind == "broad" and cause is None


def test_new_element_is_held_to_the_baseline_rate():
    totals = A.Totals(n0=1000, x0=300, n1=1000, x1=210)
    els = [A.Element("1.7.2", 1000, 300, 250, 75), A.Element("1.8.0", 0, 0, 750, 135)]
    dec = A.decompose("app_version", els, totals)
    by = {r.value: r for r in dec.rows}
    assert by["1.8.0"].ep == pytest.approx(1.0)  # the new version explains the whole drop
    assert by["1.7.2"].contribution == pytest.approx(0.0, abs=1e-12)


def test_demo_d1_drop_is_attributed_to_version_1_8_0(demo_report):
    """Platform-only cohorts hid the cause (research 5.1); the cohort_dims pull exposes it."""
    ins = next(i for i in demo_report.insights if i.id == "anomaly:quartz_merge:d1_retention_drop")
    assert ins.template == "retention_drop_cause"
    f = ins.facts()
    assert f["cause_dim"].value == "app_version" and f["cause_value"].value == "1.8.0"
    assert f["cause_rate"].value < 0.6 * f["rest_rate"].value
    assert f["cause_q"].value <= 0.10
    kinds = [e.kind for e in ins.evidence]
    assert kinds == ["anomaly", "decomposition", "attribution"]
    assert any(r.action == "rollback_or_fix_version" for r in ins.recommendations)


def test_platform_only_warehouse_names_the_missing_dimensions(brain_demo, tmp_path):
    import duckdb

    from studio_pulse.brain import insights

    path = brain_demo.copy(tmp_path)
    con = duckdb.connect(str(path))
    con.execute("DELETE FROM stg_cohort_dims")
    rep = insights.run(con, as_of=brain_demo.cfg.as_of, rules=brain_demo.rules)
    con.close()
    ins = next(i for i in rep.insights if i.id == "anomaly:quartz_merge:d1_retention_drop")
    assert ins.template == "retention_drop_nodims"
    assert "country" in ins.facts()["missing_dims"].value
    assert any(r.action == "enable_cohort_dims" for r in ins.recommendations)
