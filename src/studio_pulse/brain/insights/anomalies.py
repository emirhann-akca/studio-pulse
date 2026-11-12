"""Anomalies: the alert rules (alerts.yaml) are the detector; the brain adds evidence.

Reusing alerts.evaluate keeps one definition of "unusual" (threshold, trailing
baseline with the catalog's aggregation, forecast_band with weekly
seasonality). Each fired alert becomes an insight with the series it was
judged on; a retention drop also gets its cohort explanation (cohorts.py).
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from ... import alerts as alerts_mod
from ..evidence import Insight, Recommendation
from . import cohorts
from . import common as C

RETENTION_DAY = {"d1_retention": 1, "d7_retention": 7}


def run(ctx: C.Context) -> list[C.Candidate]:
    if not ctx.rules:
        return []
    fired = alerts_mod.evaluate(ctx.con, ctx.rules)
    rules = {r.name: r for r in ctx.rules}
    out: list[C.Candidate] = []
    for a in fired:
        if ctx.blocked(a.app_id):
            continue
        rule = rules[a.rule]
        out.append(C.Candidate(None, _insight(ctx, a, rule)))
    return out


def _insight(ctx: C.Context, a: alerts_mod.Alert, rule: alerts_mod.Rule) -> Insight:
    metric = ctx.catalog.metrics.get(a.metric)
    spec = alerts_mod.METRICS[a.metric]
    unit = spec.unit
    sql = f"SELECT * FROM ({spec.sql}) WHERE app_id = ? AND value IS NOT NULL"
    params: list[Any] = [a.app_id]
    for k, v in rule.where.items():
        sql += f" AND {k} = ?"
        params.append(v)
    sql += " ORDER BY date DESC LIMIT 21"
    cols, rows = C.query(ctx.con, sql, params)
    facts: dict[str, Any] = {
        "metric": C.fact(metric.label("en") if metric else a.metric, "text"),
        "metric_tr": C.fact(metric.label("tr") if metric else a.metric, "text"),
        "value": C.fact(a.value, unit),
        "date": C.fact(a.date, "date"),
        # drop/rise rules carry threshold_pct (20 = 20 %); the others a value in the metric's unit
        "threshold": C.fact(a.threshold / 100, "pct")
        if rule.condition.endswith("_vs_baseline")
        else C.fact(a.threshold, unit),
    }
    if rule.where:
        facts["where"] = C.fact(", ".join(f"{k}={v}" for k, v in rule.where.items()), "text")
    p = z = n = None
    change = None
    if a.baseline is not None:
        facts["baseline"] = C.fact(a.baseline, unit)
        if a.baseline:
            change = (a.value - a.baseline) / abs(a.baseline)
            facts["change"] = C.fact(change, "pct")
    if rule.condition.endswith("_vs_baseline"):
        facts["baseline_days"] = C.fact(rule.baseline_days, "id")
        facts["baseline_kind"] = C.fact(ctx.catalog.alert_baseline(a.metric), "text")
    if unit == "pct" and a.baseline and 0 < a.baseline < 1:
        sample_col = cols.index("sample") if "sample" in cols else None
        n = sum((r[sample_col] or 0) for r in rows[: rule.evaluate_days] if sample_col is not None)
        if n:
            z = (a.value - a.baseline) / math.sqrt(a.baseline * (1 - a.baseline) / n)
            p = C.two_sided_p_from_z(z)
            facts["z"] = C.fact(z, "z")
            facts["n"] = C.fact(n, "count")
    ev = C.make_evidence(
        "anomaly",
        a.app_id,
        f"{a.rule}:{a.date}",
        f"alert rule {a.rule} ({rule.condition}); series from alerts.METRICS[{a.metric}]",
        sql=sql,
        params=params,
        columns=cols,
        rows=rows,
        facts=facts,
        n=n,
        effect=change,
        effect_unit="pct" if change is not None else None,
        p=p,
        metric=a.metric,
        definition=metric.definition if metric else spec.description,
        aggregation=ctx.catalog.alert_baseline(a.metric),
        as_of=ctx.as_of,
    )
    evidence = [ev]
    template = {
        "drop_vs_baseline": "anomaly_drop",
        "rise_vs_baseline": "anomaly_rise",
        "forecast_band": "anomaly_band",
        "above": "anomaly_above",
        "below": "anomaly_below",
    }[rule.condition]
    recs = [Recommendation("investigate_anomaly", [ev.id])]
    causal = True
    day_n = RETENTION_DAY.get(a.metric)
    if day_n and rule.condition == "drop_vs_baseline":
        first_eval = a.date - dt.timedelta(days=rule.evaluate_days - 1)
        expl = cohorts.explain(
            ctx,
            a.app_id,
            day_n,
            (first_eval, a.date),
            (first_eval - dt.timedelta(days=rule.baseline_days), first_eval - dt.timedelta(days=1)),
        )
        if expl is not None:
            evidence += expl.evidence
            template = {
                "pair": "retention_drop_cause",
                "single": "retention_drop_cause",
                "broad": "retention_drop_broad",
                "no_dims": "retention_drop_nodims",
            }[expl.verdict]
            ids = [e.id for e in expl.evidence]
            if expl.cause is not None and "app_version" in expl.cause.dimension:
                recs = [Recommendation("rollback_or_fix_version", ids), *recs]
            elif expl.cause is not None:
                recs = [Recommendation("investigate_segment", ids), *recs]
            if expl.verdict == "no_dims":
                recs.append(Recommendation("enable_cohort_dims", ids))
            causal = False  # a cohort split is an association in time, not an experiment
    severity = a.severity
    score = abs(change or 0) * 10 + (abs(z) if z else 0)
    return Insight(
        id=f"anomaly:{a.app_id}:{a.rule}",
        kind="anomaly",
        app_id=a.app_id,
        severity=severity,
        template=template,
        evidence=evidence,
        recommendations=recs,
        causal=causal,
        score=score,
        p=p,
    )
