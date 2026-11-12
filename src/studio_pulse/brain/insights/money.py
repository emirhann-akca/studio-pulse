"""Monetisation shifts: the last 7 days against the 28 before.

ARPDAU and eCPM are compared on their daily values (Welch t; a week of daily
values is a small sample, which is the point: a lucky Saturday is not a
shift); payer conversion is a proportion of user-days (two-proportion z).
Reported from a 10% relative move with q <= 0.10. The headline numbers are
pooled (sum of revenue / sum of DAU), as in the catalog and fct_app_summary.
"""

from __future__ import annotations

from typing import Any

from .. import stats
from ..evidence import Insight, Recommendation
from . import common as C

MIN_REL = 0.10
RECENT = 7
PRIOR = 28

DAILY_SQL = """WITH last AS (SELECT MAX(date) AS d FROM fct_app_daily WHERE app_id = ? AND dau > 0)
SELECT date, CASE WHEN date > last.d - ? THEN 1 ELSE 0 END AS recent,
       dau, revenue_usd, payers, ad_impressions, admob_earnings_usd, ecpm_usd
FROM fct_app_daily, last
WHERE app_id = ? AND dau > 0 AND date > last.d - ? AND date <= last.d
ORDER BY date"""


def run(ctx: C.Context) -> list[C.Candidate]:
    out: list[C.Candidate] = []
    if not C.has_table(ctx.con, "fct_app_daily"):
        return out
    for app in ctx.apps:
        if ctx.blocked(app):
            continue
        params = [app, RECENT, app, RECENT + PRIOR]
        cols, rows = C.query(ctx.con, DAILY_SQL, params)
        i = {c: k for k, c in enumerate(cols)}
        rec = [r for r in rows if r[i["recent"]] == 1]
        pri = [r for r in rows if r[i["recent"]] == 0]
        if len(rec) < RECENT or len(pri) < PRIOR // 2:
            continue
        # ARPDAU: pooled headline, daily values for the test
        tests: list[tuple[str, Any]] = []
        rev_r = sum(r[i["revenue_usd"]] or 0 for r in rec)
        rev_p = sum(r[i["revenue_usd"]] or 0 for r in pri)
        dau_r = sum(r[i["dau"]] for r in rec)
        dau_p = sum(r[i["dau"]] for r in pri)
        if rev_r + rev_p > 0:
            a = [(r[i["revenue_usd"]] or 0) / r[i["dau"]] for r in pri]
            b = [(r[i["revenue_usd"]] or 0) / r[i["dau"]] for r in rec]
            w = stats.welch_mean_diff(a, b)
            pooled_p, pooled_r = rev_p / dau_p, rev_r / dau_r
            if w and pooled_p > 0:
                tests.append(("arpdau", (pooled_p, pooled_r, w[2], "usd", w[1])))
        pay_r = sum(r[i["payers"]] or 0 for r in rec)
        pay_p = sum(r[i["payers"]] or 0 for r in pri)
        if pay_r + pay_p > 0:
            t = stats.two_proportions(pay_p, dau_p, pay_r, dau_r)
            if t and t.p1 > 0:
                tests.append(("payer_conversion", (t.p1, t.p2, t.p_value, "pct", t.ci)))
        ec_r = [r[i["ecpm_usd"]] for r in rec if r[i["ecpm_usd"]] is not None]
        ec_p = [r[i["ecpm_usd"]] for r in pri if r[i["ecpm_usd"]] is not None]
        if len(ec_r) >= RECENT // 2 and len(ec_p) >= PRIOR // 4:
            imp_r = sum(r[i["ad_impressions"]] or 0 for r in rec if r[i["ecpm_usd"]] is not None)
            imp_p = sum(r[i["ad_impressions"]] or 0 for r in pri if r[i["ecpm_usd"]] is not None)
            earn_r = sum(
                r[i["admob_earnings_usd"]] or 0 for r in rec if r[i["ecpm_usd"]] is not None
            )
            earn_p = sum(
                r[i["admob_earnings_usd"]] or 0 for r in pri if r[i["ecpm_usd"]] is not None
            )
            w = stats.welch_mean_diff(ec_p, ec_r)
            if w and imp_r and imp_p and earn_p:
                tests.append(
                    ("ecpm_usd", (earn_p * 1000 / imp_p, earn_r * 1000 / imp_r, w[2], "usd", w[1]))
                )
        for metric_id, (before, after, p, unit, ci) in tests:
            rel = after / before - 1 if before else 0.0
            if abs(rel) < MIN_REL:
                out.append(C.Candidate(p, None))
                continue
            metric = ctx.catalog.get(metric_id)
            facts = {
                "metric": C.fact(metric.label("en"), "text"),
                "metric_tr": C.fact(metric.label("tr"), "text"),
                "before": C.fact(before, unit),
                "after": C.fact(after, unit),
                "rel": C.fact(rel, "pct"),
                "recent_days": C.fact(RECENT, "id"),
                "prior_days": C.fact(PRIOR, "id"),
                "dau_recent": C.fact(dau_r, "count"),
            }
            ev = C.make_evidence(
                "monetization_shift",
                app,
                metric_id,
                "last 7 days vs the 28 before: Welch t on daily values (ARPDAU, eCPM) or "
                "two-proportion z on user-days (payer conversion); headline values pooled",
                sql=DAILY_SQL,
                params=params,
                columns=cols,
                rows=rows[-(RECENT + 7) :],
                facts=facts,
                n=dau_r,
                effect=rel,
                effect_unit="pct",
                ci=ci if unit == "pct" else None,
                p=p,
                metric=metric_id,
                aggregation=metric.aggregation,
                definition=metric.definition,
                as_of=ctx.as_of,
            )
            drop = rel < 0
            out.append(
                C.Candidate(
                    p,
                    Insight(
                        id=f"monetization_shift:{app}:{metric_id}",
                        kind="monetization_shift",
                        app_id=app,
                        severity="warning" if drop else "info",
                        template="money_drop" if drop else "money_rise",
                        evidence=[ev],
                        recommendations=[Recommendation("check_monetization", [ev.id])],
                        causal=False,
                        score=abs(rel) * 10,
                        p=p,
                    ),
                )
            )
    return out
