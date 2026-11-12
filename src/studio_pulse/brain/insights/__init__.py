"""Deterministic insight jobs (B2) and their run: gate, jobs, BH, rank, store.

Order: the data-trust gate first (a blocked app gets a data card and nothing
else), then the jobs. Every hypothesis a job tested, reported or not, enters
one Benjamini-Hochberg pass at q = 0.10, so scanning 60 levels and 9 tiers
does not manufacture findings. Alerts and the trust gate are rules, not
tests, and pass through.
"""

from __future__ import annotations

import datetime as dt
import math
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb

from .. import stats
from ..catalog import Catalog, load
from ..evidence import Insight, rank, store
from . import anomalies, common, levels, money, releases, segments, trust

JOBS: list[tuple[str, Callable[[common.Context], list[common.Candidate]]]] = [
    ("anomalies", anomalies.run),
    ("difficult_levels", levels.difficult),
    ("level_survival", levels.survival),
    ("perf_regressions", releases.perf),
    ("crash_regressions", releases.crashes),
    ("monetization", money.run),
    ("segments", segments.run),
]

# Within a severity, release-related regressions outrank long-standing design findings.
KIND_PRIORITY = {
    "data_trust": 100.0,
    "crash_regression": 50.0,
    "anomaly": 40.0,
    "perf_regression": 30.0,
    "monetization_shift": 25.0,
    "difficult_level": 10.0,
    "segment": 5.0,
    "level_survival": 1.0,
}


@dataclass
class Report:
    insights: list[Insight]
    tested: int = 0  # hypotheses in the BH family
    errors: dict[str, str] = field(default_factory=dict)
    as_of: dt.date | None = None


def run(
    con: duckdb.DuckDBPyConnection,
    *,
    as_of: dt.date | None = None,
    rules: list[Any] | None = None,
    catalog: Catalog | None = None,
) -> Report:
    ctx = common.Context(
        con=con,
        as_of=as_of or dt.date.today(),
        catalog=catalog or load(),
        rules=list(rules or []),
    )
    ctx.apps = common.app_ids(con)
    ctx.trust = trust.gate(ctx)
    out: list[Insight] = trust.insights(ctx.trust)
    cands: list[common.Candidate] = []
    errors: dict[str, str] = {}
    for name, job in JOBS:
        try:
            cands += job(ctx)
        except Exception as exc:  # a broken job must not hide the others
            errors[name] = f"{type(exc).__name__}: {exc}"
            errors[name + ":trace"] = traceback.format_exc(limit=3)
    for ins in out:
        ins.score += KIND_PRIORITY.get(ins.kind, 0)
    qs = stats.benjamini_hochberg([c.p for c in cands])
    for c, q in zip(cands, qs, strict=True):
        ins = c.insight
        if ins is None:
            continue
        if c.p is not None:
            ins.q = q
            ins.main.q = q
            if q is None or q > common.Q_LEVEL:
                continue
            conf = min(1.0, -math.log10(max(q, 1e-12)) / 3)
            ins.score *= 0.5 + 0.5 * conf
        ins.score += KIND_PRIORITY.get(ins.kind, 0)
        if ctx.small(ins.app_id):
            for ev in ins.evidence:
                if "small_sample" not in ev.caveats:
                    ev.caveats.append("small_sample")
        out.append(ins)
    return Report(rank(out), sum(1 for c in cands if c.p is not None), errors, ctx.as_of)


def refresh(
    con: duckdb.DuckDBPyConnection,
    *,
    as_of: dt.date | None = None,
    rules: list[Any] | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Run the jobs and store them in meta_insights; never fails the caller's refresh."""
    try:
        rep = run(con, as_of=as_of, rules=rules)
        store(con, rep.insights, rep.as_of)
    except Exception as exc:  # the dashboard and alerts must not depend on the brain
        log(f"Insights skipped: {type(exc).__name__}: {exc}")
        return -1
    errs = [k for k in rep.errors if not k.endswith(":trace")]
    note = f" ({len(errs)} job error(s): {', '.join(errs)})" if errs else ""
    log(f"Insights: {len(rep.insights)} stored from {rep.tested} tested hypotheses{note}.")
    return len(rep.insights)
