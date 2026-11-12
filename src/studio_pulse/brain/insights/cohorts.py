"""Why did a retention rate move? Mix/rate, Adtributor and DIFF-lite on cohort cells.

With the cohort_dims pull (stg_cohort_dims) the dimensions are platform,
country, app version, first-touch source and device tier, and every pair of
them. Without it only the platform split of stg_cohort_days exists, and the
evidence says which dimensions are missing and how to get them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .. import attribution as A
from .. import stats
from ..cohort_dims import DIMS
from ..evidence import Evidence
from . import common as C

DIMS_SQL = """SELECT grouping_set, platform, country, app_version, network, device_tier,
       CASE WHEN cohort_date >= ? THEN 1 ELSE 0 END AS period,
       SUM(users) AS users, SUM(d{n}_users) AS retained
FROM stg_cohort_dims
WHERE app_id = ? AND cohort_date BETWEEN ? AND ? AND d{n}_users IS NOT NULL AND grouping_set <> ''
GROUP BY ALL"""

TOTALS_SQL = """SELECT CASE WHEN cohort_date >= ? THEN 1 ELSE 0 END AS period,
       SUM(cohort_size) AS users, SUM(retained_users) AS retained
FROM fct_retention
WHERE app_id = ? AND day_n = ? AND is_complete AND cohort_date BETWEEN ? AND ?
GROUP BY 1"""

PLATFORM_SQL = """SELECT platform, CASE WHEN cohort_date >= ? THEN 1 ELSE 0 END AS period,
       SUM(CASE WHEN day_n = 0 THEN users END) AS users,
       SUM(CASE WHEN day_n = ? THEN users END) AS retained
FROM stg_cohort_days
WHERE app_id = ? AND cohort_date BETWEEN ? AND ?
GROUP BY 1, 2"""


@dataclass
class Explanation:
    verdict: str  # pair | single | broad | no_dims
    evidence: list[Evidence] = field(default_factory=list)
    cause: A.Cell | None = None
    missing_dims: list[str] = field(default_factory=list)


def _elements(rows: list[list[Any]], cols: list[str], dims: tuple[str, ...]) -> list[A.Element]:
    acc: dict[str, list[float]] = {}
    idx = {c: i for i, c in enumerate(cols)}
    for r in rows:
        value = " | ".join(str(r[idx[d]]) for d in dims)
        a = acc.setdefault(value, [0.0, 0.0, 0.0, 0.0])
        k = 2 if r[idx["period"]] == 1 else 0
        a[k] += float(r[idx["users"]] or 0)
        a[k + 1] += float(r[idx["retained"]] or 0)
    return [A.Element(v, a[0], a[1], a[2], a[3]) for v, a in sorted(acc.items())]


def explain(
    ctx: C.Context,
    app_id: str,
    day_n: int,
    current: tuple[dt.date, dt.date],
    baseline: tuple[dt.date, dt.date],
) -> Explanation | None:
    con = ctx.con
    cur_start = current[0]
    tparams = [cur_start, app_id, day_n, baseline[0], current[1]]
    _, trows = C.query(con, TOTALS_SQL, tparams)
    t = {int(r[0]): (float(r[1] or 0), float(r[2] or 0)) for r in trows}
    if 0 not in t or 1 not in t or not t[0][0] or not t[1][0]:
        return None
    totals = A.Totals(t[0][0], t[0][1], t[1][0], t[1][1])
    facts_common = {
        "r0": C.fact(totals.r0, "pct"),
        "r1": C.fact(totals.r1, "pct"),
        "delta": C.fact(totals.delta, "pp"),
        "n0": C.fact(totals.n0, "count"),
        "n1": C.fact(totals.n1, "count"),
        "day_n": C.fact(day_n, "id"),
        "base_start": C.fact(baseline[0], "date"),
        "base_end": C.fact(baseline[1], "date"),
        "cur_start": C.fact(current[0], "date"),
        "cur_end": C.fact(current[1], "date"),
    }
    have_dims = C.has_table(con, "stg_cohort_dims")
    rows: list[list[Any]] = []
    cols: list[str] = []
    sql = DIMS_SQL.format(n=day_n)
    params = [cur_start, app_id, baseline[0], current[1]]
    if have_dims:
        cols, rows = C.query(con, sql, params)
    if not rows:
        return _platform_only(ctx, app_id, day_n, current, baseline, totals, facts_common)

    by_set: dict[str, list[list[Any]]] = {}
    gi = cols.index("grouping_set")
    for r in rows:
        by_set.setdefault(r[gi], []).append(r)
    decomps: list[A.Decomposition] = []
    cells: dict[str, list[A.Element]] = {}
    for gset, grows in by_set.items():
        dims = tuple(gset.split(","))
        els = _elements(grows, cols, dims)
        cells[gset] = els
        if len(dims) == 1:
            decomps.append(A.decompose(gset, A.with_other(els, totals), totals))
    candidates = A.adtributor(decomps)
    scanned = A.diff_cells(cells, totals)
    for c, q in zip(scanned, stats.benjamini_hochberg([c.p for c in scanned]), strict=True):
        c.q = q
    confirmed = A.confirmed(scanned, C.Q_LEVEL)
    kind, cause, cand = A.verdict(candidates, confirmed)
    if totals.delta >= 0:  # the jobs explain drops; a rise has no "cause" to name here
        kind, cause = "broad", None

    if cause is not None:
        dec_dim = cause.dimension.split(",")[0]
    elif candidates:
        dec_dim = candidates[0].dimension
    else:
        dec_dim = max(decomps, key=lambda d: sum(r.surprise for r in d.rows)).dimension
    dec = next(d for d in decomps if d.dimension == dec_dim)
    dec_ev = _decomp_evidence(app_id, day_n, dec, totals, facts_common, sql, params, cols)
    attr_facts: dict[str, Any] = {
        "cells_scanned": C.fact(len(scanned), "count"),
        "min_support": C.fact(A.MIN_SUPPORT, "pct"),
        "min_gap": C.fact(A.MIN_GAP, "pct"),
        "q_level": C.fact(C.Q_LEVEL, "ratio"),
    }
    if cand is None and candidates:
        cand = candidates[0]
    if cand is not None:
        attr_facts["cand_dim"] = C.fact(cand.dimension, "text")
        attr_facts["cand_values"] = C.fact(", ".join(cand.values), "version")
        attr_facts["cand_ep"] = C.fact(cand.ep, "pct")
    if cause is not None:
        attr_facts.update(
            cause_dim=C.fact(cause.dimension.replace(",", " × "), "text"),
            cause_value=C.fact(cause.value, "version"),
            cause_rate=C.fact(cause.rate, "pct"),
            rest_rate=C.fact(cause.rest_rate, "pct"),
            cause_gap=C.fact(cause.gap, "pct"),
            cause_share=C.fact(cause.support, "pct"),
            cause_n=C.fact(cause.n, "count"),
            cause_q=C.fact(cause.q, "q"),
        )
        if cause.rate0 is not None:
            attr_facts["cause_rate0"] = C.fact(cause.rate0, "pct")
        row = next((r for r in dec.rows if r.value == cause.value), None)
        if row is not None and "," not in cause.dimension:
            attr_facts["cause_ep"] = C.fact(row.ep, "pct")
    top = sorted(scanned, key=lambda c: (c.p, c.gap))[:12]  # worse cells first on ties
    attr_ev = C.make_evidence(
        "attribution",
        app_id,
        f"d{day_n}:{current[1]}",
        "Adtributor (EP >= 10%, cumulative EP >= 67%, ranked by Jensen-Shannon surprise) "
        "over single dimensions + DIFF-lite: each cell's current-period retention vs the rest "
        "(two-proportion z, Benjamini-Hochberg q <= 0.10, support >= 5%, gap >= 15%; pairs only "
        "when worse than each parent)",
        sql=sql,
        params=params,
        columns=["cells", "value", "users", "rate", "rest_rate", "gap", "support", "p", "q"],
        rows=[
            [c.dimension, c.value, c.n, c.rate, c.rest_rate, c.gap, c.support, c.p, c.q]
            for c in top
        ],
        facts={**facts_common, **attr_facts},
        n=totals.n1,
        effect=cause.gap if cause else None,
        effect_unit="pct" if cause else None,
        p=cause.p if cause else None,
        q=cause.q if cause else None,
        metric=f"d{day_n}_retention",
        caveats=["association_in_time"] + (["pair_cause"] if kind == "pair" else []),
        as_of=ctx.as_of,
    )
    return Explanation(kind, [dec_ev, attr_ev], cause)


def _decomp_evidence(
    app_id: str,
    day_n: int,
    dec: A.Decomposition,
    totals: A.Totals,
    facts_common: dict[str, Any],
    sql: str,
    params: list[Any],
    cols: list[str],
) -> Evidence:
    facts = {
        **facts_common,
        "dimension": C.fact(dec.dimension, "text"),
        "mix": C.fact(dec.mix, "pp"),
        "rate": C.fact(dec.rate, "pp"),
    }
    return C.make_evidence(
        "decomposition",
        app_id,
        f"d{day_n}:{dec.dimension}:{facts_common['cur_end'].value}",
        "Kitagawa mix/rate decomposition (symmetric, no residual) of the pooled rate change; "
        "per element the exact split at the current mix, a new element held to the baseline rate",
        sql=sql,
        params=params,
        columns=[
            "value",
            "share0",
            "share1",
            "rate0",
            "rate1",
            "rate_effect",
            "mix_effect",
            "contribution",
            "ep",
        ],  # fmt: skip
        rows=[
            [
                r.value,
                r.share0,
                r.share1,
                r.rate0,
                r.rate1,
                r.rate_effect,
                r.mix_effect,
                r.contribution,
                r.ep,
            ]
            for r in dec.rows
        ],  # fmt: skip
        facts=facts,
        n=totals.n1,
        effect=dec.total,
        effect_unit="pp",
        metric=f"d{day_n}_retention",
        aggregation="pooled",
    )


def _platform_only(
    ctx: C.Context,
    app_id: str,
    day_n: int,
    current: tuple[dt.date, dt.date],
    baseline: tuple[dt.date, dt.date],
    totals: A.Totals,
    facts_common: dict[str, Any],
) -> Explanation:
    params = [current[0], day_n, app_id, baseline[0], current[1]]
    cols, rows = (
        C.query(ctx.con, PLATFORM_SQL, params)
        if C.has_table(ctx.con, "stg_cohort_days")
        else ([], [])
    )
    els = _elements(rows, cols, ("platform",)) if rows else []
    dec = A.decompose("platform", A.with_other(els, totals), totals)
    ev = _decomp_evidence(app_id, day_n, dec, totals, facts_common, PLATFORM_SQL, params, cols)
    missing = [d for d in DIMS if d != "platform"]
    ev.facts["missing_dims"] = C.fact(", ".join(missing), "text")
    ev.caveats.append("dims_missing")
    return Explanation("no_dims", [ev], None, missing)
