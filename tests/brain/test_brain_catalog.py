"""B0: the metric catalog resolves against the warehouse and agrees with alerts.py."""

from __future__ import annotations

import math
import re
from pathlib import Path

import duckdb
import pytest

from studio_pulse import alerts
from studio_pulse.brain import catalog
from studio_pulse.brain.catalog import CatalogError, normalize

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def wh(brain_demo):
    con = duckdb.connect(str(brain_demo.warehouse), read_only=True)
    yield con
    con.close()


def test_catalog_loads_with_names_in_both_languages():
    cat = catalog.load()
    assert len(cat.metrics) >= 50
    for m in cat.metrics.values():
        assert m.name["en"] and m.name["tr"], m.id
        assert m.unit in catalog.UNITS and m.aggregation in catalog.AGGREGATIONS
        if m.aggregation == "pooled":
            assert m.numerator and m.denominator, m.id


def test_every_metric_resolves_to_real_columns(wh):
    for m in catalog.load().metrics.values():
        exprs = [m.value, *(x for x in (m.numerator, m.denominator, m.sample) if x), *m.dims]
        if m.time:
            exprs.append(m.time)
        sql = f"SELECT {', '.join(exprs)} FROM {m.model} WHERE {m.where()} LIMIT 1"
        wh.execute(sql)  # raises on an unknown table or column
        if m.aggregation != "latest":
            sql, params = m.period_sql()
            wh.execute(sql, params).fetchall()


def test_alert_series_are_the_catalog_series(wh):
    """alerts.METRICS keeps its own SQL; on the demo it must give the catalog's rows."""
    cat = catalog.load()
    assert set(alerts.METRICS) <= set(cat.metrics)
    for mid, spec in alerts.METRICS.items():
        m = cat.get(mid)
        assert m.is_alert_metric, mid
        assert m.unit == spec.unit, mid
        assert tuple(m.alert_dims or ()) == spec.dims, mid
        a = wh.execute(f"SELECT * FROM ({spec.sql}) ORDER BY ALL").fetchall()
        b = wh.execute(f"SELECT * FROM ({m.series_sql()}) ORDER BY ALL").fetchall()
        assert len(a) == len(b), mid
        for ra, rb in zip(a, b, strict=True):
            for x, y in zip(ra, rb, strict=True):
                if isinstance(x, float) or isinstance(y, float):
                    assert (x is None and y is None) or math.isclose(
                        float(x), float(y), rel_tol=1e-9
                    ), mid
                else:
                    assert x == y, mid


def test_pooled_alert_baselines_weight_by_the_denominator():
    """`pooled` is only allowed where the alert series' sample is the ratio's denominator."""
    for m in catalog.load().metrics.values():
        if m.alert_baseline == "pooled":
            assert m.aggregation == "pooled", m.id
            assert m.sample == m.denominator, m.id


def test_metrics_md_references_are_in_the_catalog():
    cat = catalog.load()
    covered = set(cat.not_metrics)
    for m in cat.metrics.values():
        for expr in (m.value, m.numerator or "", m.denominator or "", m.sample or ""):
            for col in re.findall(r"[a-z_][a-z0-9_]*", expr):
                covered.add(f"{m.model}.{col}")
    text = (ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8")
    refs = set(re.findall(r"`((?:fct|stg)_[a-z0-9_]+\.[a-z0-9_]+)`", text))
    level_cols = {"win_rate", "attempts_per_completion", "users_reached", "churn_rate"}
    refs |= {f"fct_level_funnel.{c}" for c in level_cols}
    assert refs, "METRICS.md references not found"
    missing = sorted(refs - covered)
    assert not missing, f"METRICS.md metrics missing from metrics.yaml: {missing}"


def test_d1_baseline_is_pooled_in_alerts_and_brain(brain_demo, demo_report, wh):
    """The research's 26.76% (mean of daily) vs 26.79% (pooled): one definition now."""
    fired = [a for a in alerts.evaluate(wh, brain_demo.rules) if a.rule == "d1_retention_drop"]
    assert [a.app_id for a in fired] == ["quartz_merge"]
    a = fired[0]
    pooled = wh.execute(
        "SELECT SUM(retained_users)::DOUBLE / SUM(cohort_size), AVG(retention) FROM fct_retention "
        "WHERE app_id = 'quartz_merge' AND day_n = 1 AND is_complete "
        "AND cohort_date BETWEEN ? - 14 AND ? - 1",
        [a.date, a.date],
    ).fetchone()
    assert a.baseline == pytest.approx(pooled[0], rel=1e-12)
    assert abs(pooled[0] - pooled[1]) > 1e-5  # the two definitions really differ
    assert "pooled rate" in a.message
    ins = next(i for i in demo_report.insights if i.id == "anomaly:quartz_merge:d1_retention_drop")
    facts = ins.facts()
    assert facts["baseline"].value == pytest.approx(pooled[0], rel=1e-12)
    assert facts["r0"].value == pytest.approx(
        pooled[0], rel=1e-12
    )  # the cohort explanation's baseline


def test_names_and_synonyms_resolve_in_both_languages():
    cat = catalog.load()
    assert cat.find("What is the D1 retention?")["metric"][0] == "d1_retention"
    assert cat.find("D1 elde tutma ne oldu?")["metric"][0] == "d1_retention"
    assert cat.find("ÇÖKMESİZ KULLANICI oranı")["metric"][0] == "crash_free_users"
    assert cat.find("en zor seviyeler")["metric"][0] == "level_fail_rate"
    assert (
        "country" in cat.find("ülkeye göre D7")["dimension"]
        or "country" in cat.find("D7 by country")["dimension"]
    )
    assert normalize("İSTANBUL ışık Çağrı") == "istanbul isik cagri"


def test_catalog_rejects_bad_definitions():
    good = {"name": {"en": "x", "tr": "x"}, "unit": "pct", "model": "t", "value": "v",
            "aggregation": "pooled", "numerator": "a", "denominator": "b", "definition": "d"}  # fmt: skip
    catalog.parse({"metrics": {"ok": good}})
    for bad in (
        {**good, "unit": "furlongs"},
        {**good, "aggregation": "median_of_vibes"},
        {k: v for k, v in good.items() if k != "numerator"},
        {**good, "model": "t; DROP TABLE x"},
        {**good, "colour": "red"},
        {**good, "alert": {"baseline": "pooled"}},  # pooled baseline without a sample
    ):
        with pytest.raises(CatalogError):
            catalog.parse({"metrics": {"m": bad}})
