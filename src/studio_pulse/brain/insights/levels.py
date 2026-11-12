"""Puzzle levels: difficulty spikes and the level funnel as a survival curve.

Difficulty spike (Roohi et al., CHI PLAY 2020; the research's query, run on
the demo): among levels with at least 200 attempts, a level whose fail rate
is at least 3 robust standard deviations (1.4826 MAD) above the app's median
level AND whose Wilson 95% lower bound is above that median. The tested
hypothesis for the run's Benjamini-Hochberg pass is the one-sample binomial
z of the level's fail rate against the median. Churn at the level (players
who stopped there and stayed away for the churn window) is reported beside
it, because a hard level that nobody quits at is a design choice, not a leak.

Survival: fct_level_funnel is a discrete survival table: at level k,
users_reached are at risk, users_churned are events, and players who stopped
at k but are still active are censored. Kaplan-Meier with Greenwood
intervals gives the share of starters still playing after each level.
"""

from __future__ import annotations

import math
from typing import Any

from .. import stats
from ..evidence import Insight, Recommendation
from . import common as C

MIN_ATTEMPTS = 200
MIN_ROBUST_Z = 3.0

LEVELS_SQL = """WITH lv AS (
  SELECT app_id, level, completes + fails AS n, fails,
         fails::DOUBLE / NULLIF(completes + fails, 0) AS fail_rate,
         churn_rate, users_reached, users_churned, attempts_per_completion
  FROM fct_level_funnel
  WHERE app_id = ? AND completes + fails >= ?
), med AS (
  SELECT median(fail_rate) AS med_fail, median(churn_rate) AS med_churn FROM lv
), mad AS (
  SELECT median(abs(fail_rate - med_fail)) * 1.4826 AS s FROM lv, med
)
SELECT lv.level, lv.n, lv.fails, lv.fail_rate, med.med_fail, mad.s AS mad_scale,
       (lv.fail_rate - med.med_fail) / NULLIF(mad.s, 0) AS robust_z,
       lv.churn_rate, med.med_churn, lv.users_reached, lv.users_churned,
       lv.attempts_per_completion
FROM lv, med, mad
ORDER BY lv.level"""

SURVIVAL_SQL = """SELECT level, users_reached AS at_risk, users_churned AS events,
       users_max_here - users_churned AS censored
FROM fct_level_funnel
WHERE app_id = ? AND users_reached > 0 AND level IS NOT NULL
ORDER BY level"""


def _apps_with_levels(ctx: C.Context) -> list[str]:
    if not C.has_table(ctx.con, "fct_level_funnel"):
        return []
    _, rows = C.query(
        ctx.con,
        "SELECT app_id FROM fct_level_funnel GROUP BY 1 HAVING SUM(completes + fails) > 0 ORDER BY 1",
    )
    return [r[0] for r in rows if not ctx.blocked(r[0])]


def difficult(ctx: C.Context) -> list[C.Candidate]:
    out: list[C.Candidate] = []
    for app in _apps_with_levels(ctx):
        params = [app, MIN_ATTEMPTS]
        cols, rows = C.query(ctx.con, LEVELS_SQL, params)
        if len(rows) < 5:
            continue
        i = {c: k for k, c in enumerate(cols)}
        for r in rows:
            n, fails, rate, med = r[i["n"]], r[i["fails"]], r[i["fail_rate"]], r[i["med_fail"]]
            if not n or rate is None or med is None or not 0 < med < 1:
                continue
            z_bin = (rate - med) / math.sqrt(med * (1 - med) / n)
            p = C.two_sided_p_from_z(z_bin) if z_bin > 0 else 1.0
            lo, hi = stats.wilson(fails, n)
            rz = r[i["robust_z"]]
            flagged = rz is not None and rz >= MIN_ROBUST_Z and lo > med
            if not flagged:
                out.append(C.Candidate(p, None))
                continue
            level = r[i["level"]]
            facts = {
                "level": C.fact(level, "id"),
                "fail_rate": C.fact(rate, "pct"),
                "fail_lo": C.fact(lo, "pct"),
                "fail_hi": C.fact(hi, "pct"),
                "med_fail": C.fact(med, "pct"),
                "robust_z": C.fact(rz, "z"),
                "n": C.fact(n, "count"),
                "churn_rate": C.fact(r[i["churn_rate"]], "pct"),
                "med_churn": C.fact(r[i["med_churn"]], "pct"),
                "attempts": C.fact(r[i["attempts_per_completion"]], "ratio"),
                "min_attempts": C.fact(MIN_ATTEMPTS, "count"),
                "ci_level": C.fact(95, "id"),
            }
            churn_hi = (r[i["churn_rate"]] or 0) > (r[i["med_churn"]] or 0)
            ev = C.make_evidence(
                "difficult_level",
                app,
                level,
                "robust z (1.4826 MAD) >= 3 and Wilson 95% lower bound above the app's median "
                "level fail rate, levels with >= 200 attempts (Roohi et al., CHI PLAY 2020)",
                sql=LEVELS_SQL,
                params=params,
                columns=cols,
                rows=[r],
                facts=facts,
                n=n,
                effect=rate - med,
                effect_unit="pp",
                ci=(lo, hi),
                p=p,
                metric="level_fail_rate",
                aggregation="pooled",
                caveats=["levels_in_order", "window_start"],
                as_of=ctx.as_of,
            )
            out.append(
                C.Candidate(
                    p,
                    Insight(
                        id=f"difficult_level:{app}:{level}",
                        kind="difficult_level",
                        app_id=app,
                        severity="warning" if churn_hi else "info",
                        template="difficult_level",
                        evidence=[ev],
                        recommendations=[
                            Recommendation("reduce_difficulty", [ev.id]),
                            Recommendation("ab_test_change", [ev.id]),
                        ],
                        score=(rate - med) * 10 + (1 if churn_hi else 0),
                        p=p,
                    ),
                )
            )
    return out


def survival(ctx: C.Context) -> list[C.Candidate]:
    out: list[C.Candidate] = []
    for app in _apps_with_levels(ctx):
        params = [app]
        cols, rows = C.query(ctx.con, SURVIVAL_SQL, params)
        pts = stats.kaplan_meier(
            [(int(r[0]), float(r[1] or 0), float(r[2] or 0), float(r[3] or 0)) for r in rows]
        )
        if len(pts) < 5:
            continue
        median_level = next((p.step for p in pts if p.survival <= 0.5), None)
        hazards = [(p.step, p.events / p.at_risk if p.at_risk else 0.0, p) for p in pts]
        typical = stats.median([h for _, h, _ in hazards])
        steep = sorted(
            [h for h in hazards if h[2].at_risk >= C.MIN_N and h[1] >= 2 * typical > 0],
            key=lambda h: -h[1],
        )[:3]
        facts: dict[str, Any] = {
            "levels": C.fact(len(pts), "count"),
            "starters": C.fact(pts[0].at_risk, "count"),
            "typical_hazard": C.fact(typical, "pct"),
            "ci_level": C.fact(95, "id"),
        }
        if median_level is not None:
            mp = next(p for p in pts if p.step == median_level)
            facts.update(
                median_level=C.fact(median_level, "id"),
                s_median=C.fact(mp.survival, "pct"),
                s_lo=C.fact(mp.ci[0], "pct"),
                s_hi=C.fact(mp.ci[1], "pct"),
            )
        else:
            last = pts[-1]
            facts.update(
                last_level=C.fact(last.step, "id"),
                s_last=C.fact(last.survival, "pct"),
                s_lo=C.fact(last.ci[0], "pct"),
                s_hi=C.fact(last.ci[1], "pct"),
            )
        if steep:
            facts["steep_levels"] = C.fact(", ".join(str(s) for s, _, _ in steep), "version")
            facts["steep_hazard"] = C.fact(steep[0][1], "pct")
            facts["steep_level"] = C.fact(steep[0][0], "id")
        ev = C.make_evidence(
            "level_survival",
            app,
            "km",
            "Kaplan-Meier over levels from aggregated counts (at risk = users reached, event = "
            "churned at the level, censored = stopped there but still active), Greenwood 95% "
            "interval with the log(-log) transform",
            sql=SURVIVAL_SQL,
            params=params,
            columns=["level", "at_risk", "events", "censored", "survival", "lo", "hi"],
            rows=[
                [p.step, p.at_risk, p.events, p.censored, p.survival, p.ci[0], p.ci[1]] for p in pts
            ],
            facts=facts,
            n=pts[0].at_risk,
            metric="level_churn_rate",
            caveats=["levels_in_order", "window_start"],
            as_of=ctx.as_of,
        )
        out.append(
            C.Candidate(
                None,
                Insight(
                    id=f"level_survival:{app}",
                    kind="level_survival",
                    app_id=app,
                    severity="info",
                    template="level_survival"
                    if median_level is not None
                    else "level_survival_nomedian",
                    evidence=[ev],
                    recommendations=[],
                    score=0.1,
                ),
            )
        )
    return out
