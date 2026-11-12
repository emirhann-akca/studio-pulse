"""User-property segments (B3b): an association, never a cause.

"Users who turn the music off retain better" is Kohavi's textbook trap:
heavier users touch more settings and churn less. The job therefore reports
the raw difference AND the difference within day-0 engagement strata
(sessions 1 / 2 / 3+, Mantel-Haenszel risk difference), always with the
fixed correlational wording and an A/B test as the only way to a cause.
"""

from __future__ import annotations

from typing import Any

from .. import stats
from ..evidence import Insight, Recommendation
from . import common as C

SEG_SQL = """SELECT property, value, day0_sessions,
       SUM(users) AS users, SUM(d{n}_users) AS retained
FROM stg_segment_retention
WHERE app_id = ? AND d{n}_users IS NOT NULL
  AND cohort_date > (SELECT MAX(cohort_date) FROM stg_segment_retention
                     WHERE app_id = ? AND d{n}_users IS NOT NULL) - 28
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""


def _mh_diff(strata: list[tuple[float, float, float, float]]) -> float | None:
    """Mantel-Haenszel risk difference over strata (x0, n0, x1, n1): weights n0 n1 / (n0 + n1)."""
    num = den = 0.0
    for x0, n0, x1, n1 in strata:
        if n0 <= 0 or n1 <= 0:
            continue
        w = n0 * n1 / (n0 + n1)
        num += w * (x1 / n1 - x0 / n0)
        den += w
    return num / den if den else None


def run(ctx: C.Context) -> list[C.Candidate]:
    out: list[C.Candidate] = []
    if not C.has_table(ctx.con, "stg_segment_retention"):
        return out
    for app in ctx.apps:
        if ctx.blocked(app):
            continue
        for day_n in (1, 7):
            sql = SEG_SQL.format(n=day_n)
            params = [app, app]
            cols, rows = C.query(ctx.con, sql, params)
            props = sorted({r[0] for r in rows})
            for prop in props:
                tot = {r[1]: (r[3], r[4]) for r in rows if r[0] == prop and r[2] is None}
                vals = sorted(
                    [v for v, (n, _) in tot.items() if n >= C.MIN_N and v != "(not set)"],
                    key=lambda v: -tot[v][0],
                )[:2]
                if len(vals) < 2:
                    continue
                base_v, test_v = vals  # the more common value is the reference
                (n0, x0), (n1, x1) = tot[base_v], tot[test_v]
                t = stats.two_proportions(x0, n0, x1, n1)
                if t is None:
                    continue
                strata = []
                for s in ("1", "2", "3+"):
                    a = next(
                        (r for r in rows if r[0] == prop and r[1] == base_v and r[2] == s), None
                    )
                    b = next(
                        (r for r in rows if r[0] == prop and r[1] == test_v and r[2] == s), None
                    )
                    if a and b:
                        strata.append((float(a[4]), float(a[3]), float(b[4]), float(b[3])))
                adj = _mh_diff(strata)
                facts: dict[str, Any] = {
                    "property": C.fact(prop, "text"),
                    "value_a": C.fact(base_v, "version"),
                    "value_b": C.fact(test_v, "version"),
                    "rate_a": C.fact(t.p1, "pct"),
                    "rate_b": C.fact(t.p2, "pct"),
                    "n_a": C.fact(n0, "count"),
                    "n_b": C.fact(n1, "count"),
                    "diff": C.fact(t.diff, "pp"),
                    "diff_lo": C.fact(t.ci[0], "pp"),
                    "diff_hi": C.fact(t.ci[1], "pp"),
                    "day_n": C.fact(day_n, "id"),
                    "ci_level": C.fact(95, "id"),
                }
                if adj is not None:
                    facts["diff_adjusted"] = C.fact(adj, "pp")
                ev = C.make_evidence(
                    "segment",
                    app,
                    f"{prop}:d{day_n}",
                    "day-N retention by the day-0 value of a GA4 user property, last 28 complete "
                    "cohorts; two-proportion z, and the Mantel-Haenszel difference within day-0 "
                    "session strata (1 / 2 / 3+)",
                    sql=sql,
                    params=params,
                    columns=cols,
                    rows=[r for r in rows if r[0] == prop],
                    facts=facts,
                    n=n0 + n1,
                    effect=t.diff,
                    effect_unit="pp",
                    ci=t.ci,
                    p=t.p_value,
                    metric=f"d{day_n}_retention",
                    aggregation="pooled",
                    caveats=["correlational"],
                    as_of=ctx.as_of,
                )
                ins = Insight(
                    id=f"segment:{app}:{prop}:d{day_n}",
                    kind="segment",
                    app_id=app,
                    severity="info",
                    template="segment_correlation",
                    evidence=[ev],
                    recommendations=[Recommendation("ab_test_setting", [ev.id])],
                    causal=False,
                    score=abs(t.diff) * 10,
                    p=t.p_value,
                )
                out.append(C.Candidate(t.p_value, ins if abs(t.diff) >= 0.01 else None))
    return out
